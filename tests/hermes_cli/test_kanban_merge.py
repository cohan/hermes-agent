"""Tests for ``kanban_boards merge`` (``hermes_cli.kanban_merge``).

The defect these pin down: the 2026-10-21 copc import copied ``tasks`` rows
without their ``task_events``, so 36 cards landed with no ``created`` event and
no origin trail. A merge must therefore (a) carry the source's event history
across, (b) leave one ``imported`` event per task naming the source board, the
snapshot and the task's source ``created_at``, and (c) add nothing on a re-run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_merge as km


@pytest.fixture
def kanban_root(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    return root


def _seed_source() -> tuple[str, str]:
    """Source board with one parent/child pair, a comment and a real run."""
    kb.create_board("source", name="Source")
    with kbc.connect_closing(board="source") as conn:
        parent = kb.create_task(conn, title="parent card", assignee="coder", body="b")
        child = kb.create_task(conn, title="child card", assignee="coder", parents=[parent])
        kb.add_comment(conn, parent, "author", "a comment that must travel")
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, summary) "
            "VALUES (?, 'coder', 'done', ?, ?, 'completed', 'did the thing')",
            (parent, 1_700_000_000, 1_700_000_100),
        )
        run_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'claimed', ?, 1700000050)",
            (parent, run_id, json.dumps({"lock": "other-machine"})),
        )
        conn.commit()
    return parent, child


def _seed_target() -> str:
    kb.create_board("target", name="Target")
    with kbc.connect_closing(board="target") as conn:
        return kb.create_task(conn, title="already here")


def _events(task_id: str, board: str = "target") -> list:
    with kbc.connect_closing(board=board) as conn:
        return conn.execute(
            "SELECT kind, payload, created_at FROM task_events WHERE task_id = ? ORDER BY created_at, id",
            (task_id,),
        ).fetchall()


def test_merge_emits_one_imported_event_per_task(kanban_root):
    parent, child = _seed_source()
    _seed_target()

    res = km.merge_board(kb.kanban_db_path(board="source"),
                         source_board="source", snapshot="kanban.db.pre-merge-test",
                         board="target")

    assert sorted(res["imported"]) == sorted([parent, child])
    assert res["counts"]["imported_events"] == 2
    assert res["skipped"] == 0

    for task_id in (parent, child):
        imported = [e for e in _events(task_id) if e["kind"] == "imported"]
        assert len(imported) == 1, task_id
        payload = json.loads(imported[0]["payload"])
        assert payload["source"] == "source"
        assert payload["snapshot"] == "kanban.db.pre-merge-test"
        assert isinstance(payload["source_created_at"], int)


def test_merge_preserves_the_source_event_trail(kanban_root):
    parent, _ = _seed_source()
    _seed_target()

    km.merge_board(kb.kanban_db_path(board="source"), source_board="source", board="target")

    kinds = [e["kind"] for e in _events(parent)]
    assert "created" in kinds, "the source's created event must survive the merge"
    claimed = [e for e in _events(parent) if e["kind"] == "claimed"]
    assert len(claimed) == 1
    assert claimed[0]["created_at"] == 1700000050, "source timestamps must not be rewritten"


def test_merge_copies_comments_runs_and_links(kanban_root):
    parent, child = _seed_source()
    _seed_target()

    res = km.merge_board(kb.kanban_db_path(board="source"), source_board="source", board="target")

    assert res["counts"]["task_comments"] == 1
    assert res["counts"]["task_runs"] == 1
    assert res["counts"]["task_links"] == 1
    with kbc.connect_closing(board="target") as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_links WHERE parent_id = ? AND child_id = ?",
                            (parent, child)).fetchone()[0] == 1
        run = conn.execute("SELECT * FROM task_runs WHERE task_id = ?", (parent,)).fetchone()
        assert run["claim_lock"] is None and run["worker_pid"] is None


def test_merge_is_idempotent(kanban_root):
    _seed_source()
    _seed_target()
    src = kb.kanban_db_path(board="source")

    first = km.merge_board(src, source_board="source", board="target")
    with kbc.connect_closing(board="target") as conn:
        before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("tasks", "task_events", "task_comments", "task_runs", "task_links")]

    second = km.merge_board(src, source_board="source", board="target")

    with kbc.connect_closing(board="target") as conn:
        after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("tasks", "task_events", "task_comments", "task_runs", "task_links")]

    assert first["counts"]["tasks"] == 2
    assert second["counts"]["tasks"] == 0
    assert second["counts"]["imported_events"] == 0
    assert second["skipped"] == 2
    assert before == after, "a re-run must not write a single row"


def test_merge_leaves_existing_tasks_untouched(kanban_root):
    parent, child = _seed_source()
    _seed_target()
    with kbc.connect_closing(board="target") as conn:
        # Same id on both sides, different title: the target's copy wins.
        conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES (?, 'target copy', 'ready', 1)",
                     (child,))
        conn.commit()

    res = km.merge_board(kb.kanban_db_path(board="source"), source_board="source", board="target")

    assert res["imported"] == [parent]
    assert res["skipped"] == 1
    with kbc.connect_closing(board="target") as conn:
        assert conn.execute("SELECT title FROM tasks WHERE id = ?", (child,)).fetchone()[0] == "target copy"
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (child,)).fetchone()[0] == 0


def test_dry_run_writes_nothing(kanban_root):
    _seed_source()
    _seed_target()

    with kbc.connect_closing(board="target") as conn:
        before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

    res = km.merge_board(kb.kanban_db_path(board="source"), source_board="source",
                         board="target", dry_run=True)

    with kbc.connect_closing(board="target") as conn:
        after = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    assert res["dry_run"] is True and res["counts"]["tasks"] == 2
    assert before == after


def test_source_board_name_is_required(kanban_root):
    kb.create_board("source", name="Source")
    kb.create_board("target", name="Target")
    with pytest.raises(ValueError):
        km.merge_board(kb.kanban_db_path(board="source"), source_board="", board="target")
