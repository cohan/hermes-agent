"""Quiesce gate and copy-merge-publish for a live kanban board.

Why this module exists (investigation: card ``t_571f934c``, evidence in
``kanban-db-corruption-report.md`` §6.4, verification: card ``t_297d69e2``):

The board is written by several independent processes — the gateway
dispatcher, one worker client per card, cron/collector readers.  Both
recorded corruption incidents came from a procedure that stepped outside that
discipline: a second writer, a raw file copy, or a shutdown that never let the
WAL settle.  A board *merge* is the one operation that rewrites every page of
the file at once, so it is the operation most able to destroy live state:

* importing a card that is mid-run silently converts it to ``ready`` and
  rewrites its live run to ``released`` — a claim that never happened is
  recorded as released, and the worker that still owns it keeps writing;
* publishing new pages by rename under a live holder is the #90950
  split-brain: the holder keeps writing a deleted inode nobody will ever read;
* publishing while the old ``-wal`` still holds frames gives the new file a
  WAL that describes a different database — reported as
  "database disk image is malformed" and indistinguishable from real damage.

So this module provides the three rules every board publish must follow:

1. ``assert_quiesced`` — refuse, naming every blocking card, if anything is
   running or holds a ``claim_lock``.  Callers map ``ValueError`` to a non-zero
   exit; the message names the command to re-run.
2. ``checkpoint_and_copy`` — ``PRAGMA wal_checkpoint(TRUNCATE)`` on the live
   board, then a ``sqlite3.Connection.backup()`` page copy into a staging file
   beside it.  All merge writes go to the staging copy, never the live file.
   The checkpoint + copy discipline is the one already proven in
   ``~/scripts/kanban-recover`` (``snapshot()``); that script stays
   self-contained because it must run when the Hermes package is broken, so
   the logic is mirrored here rather than imported from a home directory.
3. ``publish`` — ``PRAGMA integrity_check`` on the staged copy, prove no other
   process holds the live file or its sidecars, prove the live ``-wal`` is
   empty, then ``os.replace``.  Sidecars are never unlinked by this module and
   the live path is never ``cp``/``mv``/rsync'd over.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "assert_path_quiesced",
    "assert_quiesced",
    "board_in_flight",
    "checkpoint_and_copy",
    "describe_blockers",
    "discard_staging",
    "integrity",
    "live_holders",
    "publish",
    "read_only_uri",
    "staging_path",
]


def read_only_uri(path: Path | str) -> str:
    return f"file:{Path(path)}?mode=ro"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    )


def _accounts_for_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _in_flight_query(conn: sqlite3.Connection) -> Optional[str]:
    """Build the in-flight query for whatever columns this board actually has.

    A board opened read-only has not been through ``_migrate_add_optional_columns``,
    so ``claim_lock`` may not exist yet; ``status`` always does.
    """
    if not _table_exists(conn, "tasks"):
        return None
    task_columns = _accounts_for_columns(conn, "tasks")
    if "status" not in task_columns:
        return None
    has_lock = "claim_lock" in task_columns
    has_runs = _table_exists(conn, "task_runs")
    run_columns = _accounts_for_columns(conn, "task_runs") if has_runs else set()
    has_open_run = has_runs and {"task_id", "status", "ended_at"} <= run_columns

    open_run = (
        "(SELECT r.id FROM task_runs r WHERE r.task_id = t.id AND r.status = 'running' "
        "AND r.ended_at IS NULL ORDER BY r.id DESC LIMIT 1)"
        if has_open_run
        else "NULL"
    )
    conditions = ["t.status = 'running'"]
    if has_lock:
        conditions.append("t.claim_lock IS NOT NULL")
    if has_open_run:
        conditions.append(
            "EXISTS (SELECT 1 FROM task_runs r WHERE r.task_id = t.id "
            "AND r.status = 'running' AND r.ended_at IS NULL)"
        )
    lock_column = "t.claim_lock" if has_lock else "NULL"
    worker_column = "t.worker_pid" if "worker_pid" in task_columns else "NULL"
    return (
        f"SELECT t.id, t.status, {lock_column} AS claim_lock, "
        f"{worker_column} AS worker_pid, {open_run} AS open_run_id "
        f"FROM tasks t WHERE {' OR '.join(conditions)} ORDER BY t.id"
    )


def board_in_flight(db_path: Path | str) -> Optional[list[dict[str, Any]]]:
    """Cards on ``db_path`` that are running or hold a claim, newest id last.

    Returns ``None`` when the file is not a kanban board (no ``tasks`` table),
    which is how the restore path decides whether this gate applies to it.
    """
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(read_only_uri(path), uri=True, timeout=30)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        try:
            query = _in_flight_query(conn)
        except sqlite3.Error:
            return None
        if query is None:
            return None
        try:
            return [dict(row) for row in conn.execute(query).fetchall()]
        except sqlite3.Error:
            return None
    finally:
        conn.close()


def describe_blockers(blockers: list[dict[str, Any]]) -> str:
    parts = []
    for card in blockers:
        state = []
        if card.get("status"):
            state.append(f"status {card['status']}")
        if card.get("claim_lock"):
            state.append(f"claim_lock {card['claim_lock']}")
        if card.get("worker_pid"):
            state.append(f"pid {card['worker_pid']}")
        if card.get("open_run_id"):
            state.append(f"run {card['open_run_id']} open")
        parts.append(f"{card.get('id')} ({', '.join(state) or 'in flight'})")
    return "; ".join(parts)


def _refusal(
    blockers: list[dict[str, Any]],
    *,
    board_label: str,
    command: str,
    action: str,
) -> str:
    plural = "card is" if len(blockers) == 1 else "cards are"
    return (
        f"refusing to {action} {board_label}: {len(blockers)} {plural} still in flight — "
        f"{describe_blockers(blockers)}. A board {action} rewrites the board file, so a "
        f"mid-run card would lose its claim and its live run would be recorded as released. "
        f"Wait for those workers to exit (`hermes kanban show <id>`), or park them "
        f"(`hermes kanban block <id> --reason \"paused for merge\"`), then re-run: {command}"
    )


def assert_quiesced(
    conn: sqlite3.Connection,
    *,
    board_label: str,
    command: str,
    action: str = "merge",
) -> None:
    """Raise ``ValueError`` naming every blocking card if the board is busy.

    The caller (``hermes kanban boards merge``) maps ``ValueError`` to exit
    code 1, so a refusal is a hard stop rather than a warning.

    Both sides of a merge are guarded: the source because importing a mid-run
    card loses that card's claim, the destination because publishing replaces
    the file those workers are writing.
    """
    query = _in_flight_query(conn)
    if query is None:
        return
    blockers = [dict(row) for row in conn.execute(query).fetchall()]
    if blockers:
        raise ValueError(
            _refusal(blockers, board_label=board_label, command=command, action=action)
        )


def assert_path_quiesced(
    db_path: Path | str,
    *,
    board_label: str,
    command: str,
    action: str = "merge",
) -> None:
    """``assert_quiesced`` for a board file that is not open in this process."""
    path = Path(db_path)
    if not path.is_file():
        return
    blockers = board_in_flight(path)
    if blockers:
        raise ValueError(
            _refusal(blockers, board_label=board_label, command=command, action=action)
        )


def integrity(path: Path | str) -> str:
    conn = sqlite3.connect(read_only_uri(path), uri=True, timeout=30)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def staging_path(live: Path) -> Path:
    """A unique staging file in the live board's own directory.

    Same directory so the publish is a same-filesystem ``os.replace`` (never a
    cross-device copy), and dot-prefixed plus process-unique so two merges
    cannot collide.
    """
    return live.parent / f".{live.name}.merge-{os.getpid()}-{uuid.uuid4().hex[:8]}.db"


def _assert_wal_empty(live: Path) -> None:
    """A non-empty ``-wal`` must never survive the publish.

    ``os.replace`` installs new pages but leaves the old ``-wal`` in place. A
    WAL carrying frames from the previous generation would be replayed onto the
    new file — "database disk image is malformed" that is indistinguishable
    from real page damage. After ``PRAGMA wal_checkpoint(TRUNCATE)`` with no
    other holder the file is zero bytes (or gone); if it is not, something is
    still reading the board and the publish must abort.
    """
    wal = Path(str(live) + "-wal")
    if not wal.exists():
        return
    size = wal.stat().st_size
    if not size:
        return
    raise ValueError(
        f"refusing to publish: {wal.name} is still {size} bytes after "
        f"PRAGMA wal_checkpoint(TRUNCATE), so another process is reading {live.name}. "
        f"Renaming a new file under a live WAL would replay the previous generation's "
        f"frames onto it. Stop that process (e.g. `hermes gateway stop`) and re-run."
    )


def checkpoint_and_copy(live: Path, staging: Path) -> None:
    """Checkpoint *live*, then copy its pages into *staging* with the backup API.

    Mirrors ``~/scripts/kanban-recover`` ``snapshot()``: the checkpoint makes
    the copy start from a settled WAL, and ``backup()`` reads through any WAL
    that remains rather than copying bytes, so the image is a consistent
    database and not a torn file.
    """
    staging.parent.mkdir(parents=True, exist_ok=True)
    live_conn = sqlite3.connect(str(live), timeout=30)
    try:
        try:
            live_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            # backup() still reads through a deep WAL; the copy stays correct,
            # and _assert_wal_empty decides whether publishing is allowed.
            pass
        staging_conn = sqlite3.connect(str(staging))
        try:
            live_conn.backup(staging_conn)
        finally:
            staging_conn.close()
    finally:
        live_conn.close()
    _assert_wal_empty(live)


def live_holders(live: Path) -> Optional[list[int]]:
    """PIDs of OTHER processes holding *live* or its sidecars open.

    ``None`` means "the scan could not run" — callers must treat that as
    "cannot prove quiesced" and refuse, never as "no holders".
    """
    from hermes_cli.backup_restore import _foreign_db_holder_pids

    return _foreign_db_holder_pids(live)


def _fsync(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _settle_staging_wal(staging: Path) -> None:
    """Fold any WAL the staging connection left behind into the staging file.

    ``os.replace`` moves the main file only. Publishing a staging file whose
    committed rows still live in ``<staging>-wal`` would drop them — the exact
    "copied the .db but not the WAL tail" failure of 2026-10-18.
    """
    wal = Path(str(staging) + "-wal")
    if not wal.exists() or not wal.stat().st_size:
        return
    conn = sqlite3.connect(str(staging), timeout=30)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    if wal.exists() and wal.stat().st_size:
        raise ValueError(
            f"refusing to publish: the staged copy {staging.name} still has "
            f"{wal.stat().st_size} bytes in its WAL and could not be checkpointed; "
            f"publishing the main file alone would drop those rows."
        )


def publish(staging: Path, live: Path, *, board_label: str) -> None:
    """Verify *staging*, prove nobody holds *live*, then publish by atomic rename."""
    _settle_staging_wal(staging)
    result = integrity(staging)
    if result != "ok":
        raise ValueError(
            f"refusing to publish the {board_label} merge: the staged copy failed "
            f"PRAGMA integrity_check ({result}). The live board was left untouched."
        )

    holders = live_holders(live)
    if holders is None:
        raise ValueError(
            f"refusing to publish the {board_label} merge: this host's /proc scan is "
            f"unavailable, so it cannot be proved that no process holds {live.name} open. "
            f"A rename under a live holder leaves it writing a deleted inode (#90950). "
            f"Re-run on the host that owns the board."
        )
    if holders:
        raise ValueError(
            f"refusing to publish the {board_label} merge: {live.name} is still held open "
            f"by PID(s) {', '.join(str(pid) for pid in sorted(holders))}. Stop them "
            f"(`hermes gateway stop`) and re-run — publishing under a live holder is the "
            f"#90950 split-brain class."
        )

    if live.exists():
        _assert_wal_empty(live)
    _fsync(staging)
    os.replace(staging, live)
    _fsync_dir(live.parent)


def discard_staging(staging: Path) -> None:
    """Remove this process's own staging artifacts, never the live board's.

    Only dot-prefixed ``*.merge-<pid>-<hex>.db`` files — plus the sidecars and
    the ``.init.lock`` that ``kanban_db_connect`` creates beside them — are
    touched.
    """
    names = (str(staging), str(staging) + "-wal", str(staging) + "-shm",
             str(staging) + ".init.lock")
    for name in names:
        candidate = Path(name)
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            # A leftover staging file is harmless; it is dot-prefixed, in the
            # board's directory, and named for this pid.
            continue
