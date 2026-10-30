"""Quiesce gate + staged publish for board merges and board restores.

Card ``t_2ed1c4a5``. Verification of the defect is in
``~/.hermes/kanban/workspaces/t_297d69e2/merge-guard-verification-report.md``:
a merge used to import a ``running`` card holding a ``claim_lock`` with no
refusal (the card landed ``ready``, its live run was rewritten to
``released``) and published in place through a live connection (destination
inode unchanged, no checkpoint, no ``backup()`` copy, no staging file).

Every test here runs against scratch databases under ``tmp_path`` with
``HERMES_KANBAN_HOME`` rebound, so the live board is never opened.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_merge as km
from hermes_cli import kanban_quiesce as kq
from hermes_cli.backup_restore import _safe_restore_db

RUNNING_LOCK = "hermes:424242"


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
    kb.create_board("source", name="Source")
    with kbc.connect_closing(board="source") as conn:
        parent = kb.create_task(conn, title="parent card", assignee="coder", body="b")
        child = kb.create_task(conn, title="child card", assignee="coder", parents=[parent])
    return parent, child


def _seed_target() -> str:
    kb.create_board("target", name="Target")
    with kbc.connect_closing(board="target") as conn:
        return kb.create_task(conn, title="already here")


def _mark_running(board: str, task_id: str, *, open_run: bool = True,
                  lock: str = RUNNING_LOCK, pid: int = 424242) -> None:
    """Put a card mid-flight exactly as a live worker leaves it."""
    with kbc.connect_closing(board=board) as conn:
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = ?, worker_pid = ? WHERE id = ?",
            (lock, pid, task_id),
        )
        if open_run:
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, claim_lock, worker_pid) "
                "VALUES (?, 'builder', 'running', ?, ?, ?)",
                (task_id, 1_700_000_000, lock, pid),
            )
        conn.commit()


def _staging_leftovers(board: str) -> list[Path]:
    target = kb.kanban_db_path(board=board)
    return sorted(target.parent.glob(f".{target.name}.merge-*"))


def _merge(**kwargs):
    return km.merge_board(kb.kanban_db_path(board="source"), source_board="source", **kwargs)


# --------------------------------------------------------------------------
# the refusal
# --------------------------------------------------------------------------

def test_merge_refuses_a_running_card_on_the_source(kanban_root):
    parent, _ = _seed_source()
    _seed_target()
    _mark_running("source", parent)
    target = kb.kanban_db_path(board="target")
    inode_before = target.stat().st_ino

    with pytest.raises(ValueError) as excinfo:
        _merge(board="target")

    message = str(excinfo.value)
    assert parent in message, "the refusal must name the blocking card"
    assert f"claim_lock {RUNNING_LOCK}" in message
    assert "hermes kanban boards merge" in message, "the refusal must name the safe command"
    assert "in flight" in message

    assert target.stat().st_ino == inode_before, "a refused merge must not publish"
    with kbc.connect_closing(board="target") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert _staging_leftovers("target") == []


def test_merge_refuses_an_unfinished_run_without_a_claim_lock(kanban_root):
    """A crashed worker leaves an open run behind; that is still work in flight."""
    parent, _ = _seed_source()
    _seed_target()
    _mark_running("source", parent)
    with kbc.connect_closing(board="source") as conn:
        # claim released, but the run never ended
        conn.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?", (parent,))
        conn.commit()
    with kbc.connect_closing(board="source") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ? AND status = 'running' "
            "AND ended_at IS NULL", (parent,)
        ).fetchone()[0] == 1

    with pytest.raises(ValueError) as excinfo:
        _merge(board="target")

    assert parent in str(excinfo.value)
    assert "run" in str(excinfo.value)


def test_merge_refuses_a_busy_destination(kanban_root):
    _seed_source()
    existing = _seed_target()
    _mark_running("target", existing)

    with pytest.raises(ValueError) as excinfo:
        _merge(board="target")

    message = str(excinfo.value)
    assert "target board" in message
    assert existing in message
    with kbc.connect_closing(board="target") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_dry_run_predicts_the_refusal(kanban_root):
    parent, _ = _seed_source()
    _seed_target()
    _mark_running("source", parent)

    with pytest.raises(ValueError):
        _merge(board="target", dry_run=True)


def test_quiesced_boards_merge_normally(kanban_root):
    parent, child = _seed_source()
    _seed_target()

    res = _merge(board="target")

    assert sorted(res["imported"]) == sorted([parent, child])
    assert res["quiesced"] is True
    assert res["counts"]["imported_events"] == 2


# --------------------------------------------------------------------------
# the publish: checkpoint + backup() copy, merge into the copy, rename
# --------------------------------------------------------------------------

def test_merge_publishes_by_atomic_rename(kanban_root):
    parent, _ = _seed_source()
    _seed_target()
    target = kb.kanban_db_path(board="target")
    inode_before = target.stat().st_ino

    res = _merge(board="target")

    assert res["published_by"] == "atomic-rename"
    assert target.stat().st_ino != inode_before, "the publish must be a rename, not an in-place write"
    assert kq.integrity(target) == "ok"
    assert _staging_leftovers("target") == [], "the staged copy must not be left beside the board"
    # Nothing of the previous WAL generation may survive onto the new file.
    wal = Path(str(target) + "-wal")
    assert (not wal.exists()) or wal.stat().st_size == 0


def test_merge_writes_into_a_copy_not_the_live_file(kanban_root, monkeypatch):
    """A failed publish leaves the live board byte-identical."""
    parent, _ = _seed_source()
    _seed_target()
    target = kb.kanban_db_path(board="target")
    before = target.read_bytes()

    def refuse_publish(staging, live, *, board_label):
        assert staging.is_file()
        assert live == target
        raise ValueError("synthetic publish refusal")

    monkeypatch.setattr(kq, "publish", refuse_publish)
    with pytest.raises(ValueError, match="synthetic publish refusal"):
        _merge(board="target")

    assert target.read_bytes() == before, "the live board must be untouched"
    assert _staging_leftovers("target") == []


def test_merge_creates_a_missing_board_by_rename(kanban_root, tmp_path):
    _seed_source()
    target = kb.kanban_db_path(board="fresh")
    assert not target.exists()

    res = km.merge_board(kb.kanban_db_path(board="source"), source_board="source", board="fresh")

    assert res["published_by"] == "atomic-rename"
    assert target.is_file()
    assert kq.integrity(target) == "ok"
    with kbc.connect_closing(board="fresh") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


# --------------------------------------------------------------------------
# the restore path
# --------------------------------------------------------------------------

def _snapshot_of(path: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(path))
    copy = sqlite3.connect(str(destination))
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    return destination


def test_restore_refuses_a_board_with_a_running_card(kanban_root, tmp_path):
    kb.create_board("live", name="Live")
    with kbc.connect_closing(board="live") as conn:
        card = kb.create_task(conn, title="mid-run")
    _mark_running("live", card)
    dst = kb.kanban_db_path(board="live")
    snapshot = _snapshot_of(dst, tmp_path / "snapshots" / "kanban.db")

    assert _safe_restore_db(snapshot, dst) is False

    with kbc.connect_closing(board="live") as conn:
        row = conn.execute("SELECT status, claim_lock FROM tasks WHERE id = ?", (card,)).fetchone()
    assert tuple(row) == ("running", RUNNING_LOCK), "a refused restore must not touch the board"


def test_restore_accepts_a_quiesced_board(kanban_root, tmp_path):
    kb.create_board("live", name="Live")
    with kbc.connect_closing(board="live") as conn:
        card = kb.create_task(conn, title="settled")
    dst = kb.kanban_db_path(board="live")
    snapshot = _snapshot_of(dst, tmp_path / "snapshots" / "kanban.db")
    with kbc.connect_closing(board="live") as conn:
        conn.execute("UPDATE tasks SET title = 'changed after the snapshot' WHERE id = ?", (card,))
        conn.commit()

    assert _safe_restore_db(snapshot, dst) is True

    with kbc.connect_closing(board="live") as conn:
        assert conn.execute("SELECT title FROM tasks WHERE id = ?", (card,)).fetchone()[0] == "settled"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def test_board_in_flight_ignores_files_that_are_not_boards(tmp_path):
    plain = tmp_path / "not-a-board.db"
    plain.write_bytes(b"")
    assert kq.board_in_flight(plain) is None
    assert kq.board_in_flight(tmp_path / "missing.db") is None


def test_board_in_flight_reports_the_open_run(kanban_root):
    kb.create_board("source", name="Source")
    with kbc.connect_closing(board="source") as conn:
        card = kb.create_task(conn, title="x")
    _mark_running("source", card)

    blockers = kq.board_in_flight(kb.kanban_db_path(board="source"))

    assert blockers and blockers[0]["id"] == card
    assert blockers[0]["claim_lock"] == RUNNING_LOCK
    assert blockers[0]["open_run_id"] is not None


def test_settled_boards_report_no_blockers(kanban_root):
    parent, _ = _seed_source()
    with kbc.connect_closing(board="source") as conn:
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at) "
                     "VALUES (?, 'builder', 'done', 1700000000, 1700000100)", (parent,))
        conn.commit()

    assert kq.board_in_flight(kb.kanban_db_path(board="source")) == []
