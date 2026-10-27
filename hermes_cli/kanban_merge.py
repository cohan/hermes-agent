"""Merge another board's rows INTO an existing board, preserving event history.

Backs ``hermes kanban boards merge``. The 2026-10-21 copc import (an ad-hoc
script, ``import_copc_recent.py``, since pruned) copied ``tasks``,
``task_links``, ``task_comments`` and ``task_runs`` but not ``task_events``,
and recorded one ``imported`` event with no source timestamp. The result: 36
cards landed with no ``created`` event and no origin trail.

This is the supported replacement. It copies the source's event trail row for
row (run ids remapped onto the target's ``task_runs``), then appends one
``imported`` event per task naming the source board, the snapshot file and the
task's source ``created_at`` — so an arrival is always auditable even when the
source itself had lost its history.

Idempotent: a task whose id already exists on the target board is skipped
entirely — no rows, no events — so re-running a merge adds nothing. Rows are
written in one transaction on a staged copy; a failure leaves the target board
untouched.

Machine-local state is never merged: claims, PIDs, heartbeats, session ids,
project links, workspace paths and branch names describe the source machine.
Tasks that were mid-run arrive ``ready``; dispatchable tasks whose workspace
cannot be rebuilt here are parked in ``triage`` (same rule as archive import).

SAFE PUBLISH (card ``t_2ed1c4a5``)
A merge is the one board operation that rewrites every page at once, so it no
longer writes through a live connection:

1. a hard quiesce gate — any card ``running``, holding a ``claim_lock`` or
   carrying an unfinished run on **either** side refuses the merge, naming the
   blocking cards (``kanban_quiesce.assert_quiesced``);
2. ``PRAGMA wal_checkpoint(TRUNCATE)`` + ``sqlite3.Connection.backup()`` copy of
   the live board into a staging file in the same directory;
3. the whole merge runs against that COPY, in one transaction;
4. ``PRAGMA integrity_check`` on the copy, then ``os.replace`` (atomic rename)
   only after proving no process holds the live board or its sidecars.

The live board is therefore never opened for writing by this module, its
sidecars are never unlinked, and its inode is only replaced while quiesced.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_quiesce as quiesce

_DISPATCHABLE_STATUSES = ("ready", "running", "todo", "scheduled")

# Columns that describe the SOURCE MACHINE's live state, not the card.
_DROP_TASK_COLUMNS = frozenset({
    "claim_lock", "claim_expires", "worker_pid", "current_run_id",
    "last_heartbeat_at", "session_id", "project_id",
    "consecutive_failures", "last_failure_error",
    "workspace_path", "branch_name",
})
# Child tables carry their own AUTOINCREMENT ids; the target allocates new
# ones and events are repointed at the new run id.
_DROP_RUN_COLUMNS = frozenset({
    "id", "claim_lock", "claim_expires", "worker_pid", "last_heartbeat_at",
})
_DROP_CHILD_ID = frozenset({"id"})


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _read_shared_columns(conn: sqlite3.Connection, src: sqlite3.Connection, table: str,
                         drop=frozenset()) -> list[str]:
    src_cols = _columns(src, table)
    target_cols = set(_columns(conn, table))
    return [c for c in src_cols if c in target_cols and c not in drop]


def _existing_ids(target_path: Path) -> set[str]:
    """Task ids already on the target board, read-only (empty when it has none)."""
    if not target_path.is_file():
        return set()
    with contextlib.closing(
        sqlite3.connect(quiesce.read_only_uri(target_path), uri=True, timeout=30)
    ) as conn:
        try:
            return {row[0] for row in conn.execute("SELECT id FROM tasks").fetchall()}
        except sqlite3.Error:
            return set()


def _dry_run_counts(src: sqlite3.Connection, ids: list[str], existing: set[str]) -> dict[str, int]:
    placeholders = ", ".join("?" * len(ids))
    events = int(src.execute(
        f"SELECT COUNT(*) FROM task_events WHERE task_id IN ({placeholders})", ids
    ).fetchone()[0])
    comments = int(src.execute(
        f"SELECT COUNT(*) FROM task_comments WHERE task_id IN ({placeholders})", ids
    ).fetchone()[0])
    runs = int(src.execute(
        f"SELECT COUNT(*) FROM task_runs WHERE task_id IN ({placeholders})", ids
    ).fetchone()[0])
    keep = existing | set(ids)
    links = sum(
        1 for link in src.execute("SELECT parent_id, child_id FROM task_links").fetchall()
        if link["parent_id"] in keep and link["child_id"] in keep
    )
    return {"tasks": len(ids), "task_events": events, "task_comments": comments,
            "task_runs": runs, "task_links": links, "imported_events": len(ids)}


def _apply_into(
    conn: sqlite3.Connection,
    src: sqlite3.Connection,
    todo: list[dict[str, Any]],
    parked: list[str],
    existing: set[str],
    *,
    source_board: str,
    label: str,
) -> dict[str, int]:
    """Write every pending task into *conn* (the staged copy) in one transaction."""
    ids = [row["id"] for row in todo]
    task_cols = _read_shared_columns(conn, src, "tasks", _DROP_TASK_COLUMNS)
    event_cols = _read_shared_columns(conn, src, "task_events", _DROP_CHILD_ID)
    comment_cols = _read_shared_columns(conn, src, "task_comments", _DROP_CHILD_ID)
    run_cols = _read_shared_columns(conn, src, "task_runs", _DROP_RUN_COLUMNS)

    events = comments = runs = links = imported_events = 0
    with kb.write_txn(conn):
        run_id_map: dict[int, int] = {}
        for row in todo:
            task_id = row["id"]
            values = [row[c] for c in task_cols]
            if "status" in task_cols and row.get("status") == "running":
                values[task_cols.index("status")] = "ready"
            if task_id in parked:
                values[task_cols.index("status")] = "triage"
            conn.execute(
                f"INSERT INTO tasks ({', '.join(task_cols)}) VALUES ({', '.join('?' * len(task_cols))})",
                values,
            )

            # Runs first: the parties' original run ids are what the events point at.
            for run in (dict(r) for r in src.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at, id", (task_id,)
            ).fetchall()):
                run_values = [run[c] for c in run_cols]
                if "status" in run_cols and run.get("status") == "running":
                    run_values[run_cols.index("status")] = "released"
                cur = conn.execute(
                    f"INSERT INTO task_runs ({', '.join(run_cols)}) "
                    f"VALUES ({', '.join('?' * len(run_cols))})",
                    run_values,
                )
                run_id_map[int(run["id"])] = int(cur.lastrowid or 0)
                runs += 1

            for comment in (dict(r) for r in src.execute(
                "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at, id", (task_id,)
            ).fetchall()):
                conn.execute(
                    f"INSERT INTO task_comments ({', '.join(comment_cols)}) "
                    f"VALUES ({', '.join('?' * len(comment_cols))})",
                    [comment[c] for c in comment_cols],
                )
                comments += 1

            source_events = [dict(r) for r in src.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at, id", (task_id,)
            ).fetchall()]
            for event in source_events:
                values = []
                for col in event_cols:
                    value = event[col]
                    if col == "run_id" and value is not None:
                        value = run_id_map.get(int(value))
                    values.append(value)
                conn.execute(
                    f"INSERT INTO task_events ({', '.join(event_cols)}) "
                    f"VALUES ({', '.join('?' * len(event_cols))})",
                    values,
                )
                events += 1

            # The arrival itself, always present and always last. Names the
            # source board, the snapshot, and when the card was created
            # there — the fields the 2026-10-21 merge did not record.
            kb._append_event(conn, task_id, "imported", {
                "source": source_board,
                "snapshot": label,
                "source_created_at": int(row["created_at"]),
                "source_events": len(source_events),
                "parked": task_id in parked,
            })
            imported_events += 1

        keep = existing | set(ids)
        for link in (dict(r) for r in src.execute(
            "SELECT parent_id, child_id FROM task_links").fetchall()):
            if link["parent_id"] in keep and link["child_id"] in keep:
                conn.execute(
                    "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (link["parent_id"], link["child_id"]),
                )
                links += 1

    return {"tasks": len(ids), "task_events": events, "task_comments": comments,
            "task_runs": runs, "task_links": links, "imported_events": imported_events}


def merge_board(
    source_db: str | Path,
    *,
    source_board: str,
    snapshot: Optional[str] = None,
    board: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Merge every task in ``source_db`` that the target board does not have.

    Returns a summary dict. ``dry_run=True`` reports the same shape without
    writing anything (the quiesce gate still applies — a dry run predicts the
    refusal instead of hiding it).
    """
    source = Path(source_db).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"source database not found: {source}")
    label = (snapshot or source.name).strip() or source.name
    source_board = (source_board or "").strip()
    if not source_board:
        raise ValueError("a source board name is required — it is recorded on every imported event")

    target_board = board or kb.get_current_board()
    target_path = kb.kanban_db_path(board=board)
    command = f"hermes kanban boards merge {source_db} --from-board {source_board}"

    warnings: list[str] = []
    with contextlib.closing(
        sqlite3.connect(quiesce.read_only_uri(source), uri=True, timeout=30)
    ) as src:
        src.row_factory = sqlite3.Row
        if not src.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone():
            raise ValueError(f"{source} is not a kanban board database (no tasks table)")

        # (1) quiesce gate — both sides. A merge wakes a mid-run card and
        # rewrites the file its worker is still writing.
        quiesce.assert_quiesced(
            src, board_label=f"source board {source_board!r}", command=command
        )
        if target_path.is_file():
            quiesce.assert_path_quiesced(
                target_path, board_label=f"target board {target_board!r}", command=command
            )

        existing = _existing_ids(target_path)
        incoming = [dict(row) for row in src.execute("SELECT * FROM tasks ORDER BY created_at, id")]
        todo = [row for row in incoming if row["id"] not in existing]
        skipped = len(incoming) - len(todo)
        ids = [row["id"] for row in todo]
        parked = [
            row["id"] for row in todo
            if row.get("workspace_kind") in ("dir", "worktree")
            and row.get("status") in _DISPATCHABLE_STATUSES
        ]

        if not todo:
            return {
                "board": target_board, "source_board": source_board, "snapshot": label,
                "dry_run": bool(dry_run), "imported": [], "skipped": skipped,
                "counts": {"tasks": 0, "task_events": 0, "task_comments": 0,
                           "task_runs": 0, "task_links": 0, "imported_events": 0},
                "parked": [], "warnings": warnings,
                "quiesced": True, "published_by": "none",
            }

        if dry_run:
            return {
                "board": target_board, "source_board": source_board, "snapshot": label,
                "dry_run": True, "imported": ids, "skipped": skipped,
                "counts": _dry_run_counts(src, ids, existing),
                "parked": parked, "warnings": warnings,
                "quiesced": True, "published_by": "none",
            }

        # (2)-(4) stage a copy, merge into it, verify, publish by atomic rename.
        staging = quiesce.staging_path(target_path)
        try:
            if target_path.is_file():
                quiesce.checkpoint_and_copy(target_path, staging)
            with kbc.connect_closing(db_path=staging) as conn:
                counts = _apply_into(
                    conn, src, todo, parked, existing,
                    source_board=source_board, label=label,
                )
            quiesce.publish(staging, target_path, board_label=f"target board {target_board!r}")
        finally:
            quiesce.discard_staging(staging)

    if parked:
        warnings.append(
            f"{len(parked)} task(s) parked in triage — their workspace was a directory or git "
            f"worktree on the source machine"
        )
    return {
        "board": target_board, "source_board": source_board, "snapshot": label,
        "dry_run": False, "imported": ids, "skipped": skipped,
        "counts": counts, "parked": parked, "warnings": warnings,
        "quiesced": True, "published_by": "atomic-rename",
    }
