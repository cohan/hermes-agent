"""Quiesce gates and atomic publish for board create/move/delete.

The board file is written by several processes.  These tests pin the two
guarantees this card adds:

* a board whose cards are still running (or whose file is held open) refuses
  create/move/delete with a message that names the blocking card/PID;
* the only way a DB file is built or a board directory leaves its live path is
  a staging file followed by an atomic rename — nothing mutates the live
  ``kanban.db`` in place and no sidecar is unlinked by hand.
"""

from __future__ import annotations

import shutil as shutil_mod
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_quiesce as quiesce


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _mark_running(board: str, *, title: str = "still running") -> str:
    """Create a card and put it in the live state the quiesce gate looks for."""
    with kbc.connect_closing(board=board) as conn:
        task_id = kb.create_task(conn, title=title, assignee="dev")
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock='test-lock' WHERE id=?",
            (task_id,),
        )
        conn.commit()
    return task_id


class TestCreateBoardGate:
    def test_refuses_when_a_card_is_running(self, fresh_home):
        kb.create_board("guarded")
        task_id = _mark_running("guarded")
        with pytest.raises(ValueError) as exc:
            kb.create_board("guarded", name="Renamed")
        message = str(exc.value)
        assert task_id in message
        assert "in flight" in message.lower()

    def test_existing_nonempty_db_is_metadata_only(self, fresh_home):
        kb.create_board("guarded")
        db = kb.kanban_db_path("guarded")
        inode = db.stat().st_ino
        mtime = db.stat().st_mtime_ns
        kb.create_board("guarded", name="New name")
        assert kb.read_board_metadata("guarded")["name"] == "New name"
        assert db.stat().st_ino == inode
        assert db.stat().st_mtime_ns == mtime

    def test_new_board_builds_db_in_staging(self, fresh_home):
        kb.create_board("fresh")
        db = kb.kanban_db_path("fresh")
        assert db.is_file() and db.stat().st_size > 0
        with kbc.connect_closing(board="fresh") as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "tasks" in tables
        # The staging file used for the build was cleaned up after publish.
        leftovers = [p.name for p in db.parent.iterdir() if ".merge-" in p.name]
        assert leftovers == []


class TestRemoveBoardGate:
    @pytest.mark.parametrize("archive", [True, False])
    def test_refuses_when_a_card_is_running(self, fresh_home, archive):
        kb.create_board("guarded")
        task_id = _mark_running("guarded")
        with pytest.raises(ValueError) as exc:
            kb.remove_board("guarded", archive=archive)
        message = str(exc.value)
        assert task_id in message
        assert "in flight" in message.lower()
        # Nothing moved or removed: the board and its DB are still in place.
        assert kb.board_exists("guarded")
        assert kb.kanban_db_path("guarded").is_file()

    @pytest.mark.parametrize("archive", [True, False])
    def test_refuses_when_a_foreign_holder_exists(
        self, fresh_home, monkeypatch, archive
    ):
        kb.create_board("held")
        monkeypatch.setattr(quiesce, "live_holders", lambda _path: [4242])
        with pytest.raises(ValueError, match="4242"):
            kb.remove_board("held", archive=archive)
        assert kb.board_exists("held")

    def test_fails_closed_when_holder_scan_unavailable(
        self, fresh_home, monkeypatch
    ):
        kb.create_board("held")
        monkeypatch.setattr(quiesce, "live_holders", lambda _path: None)
        with pytest.raises(ValueError, match="/proc scan"):
            kb.remove_board("held", archive=False)
        assert kb.board_exists("held")


class TestRemoveBoardAtomicMove:
    def test_archive_renames_the_whole_board(self, fresh_home):
        kb.create_board("moved")
        result = kb.remove_board("moved", archive=True)
        assert result["action"] == "archived"
        target = Path(result["new_path"])
        assert target.is_dir()
        assert (target / "kanban.db").is_file()
        assert (target / "board.json").is_file()
        # The original slug keeps only an archived tombstone, not a live DB.
        assert not (kb.board_dir("moved") / "kanban.db").exists()
        assert kb.read_board_metadata("moved")["archived"] is True

    def test_delete_renames_before_removing(self, fresh_home, monkeypatch):
        kb.create_board("gone")
        seen: dict[str, object] = {}
        real_rmtree = shutil_mod.rmtree

        def spy(path, *args, **kwargs):
            seen["path"] = Path(path)
            seen["names"] = sorted(p.name for p in Path(path).iterdir())
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(shutil_mod, "rmtree", spy)
        result = kb.remove_board("gone", archive=False)
        assert result["action"] == "deleted"
        assert "_trash" in str(seen["path"])
        # The whole board directory — DB and metadata — was detached before
        # anything was removed; no sidecar was unlinked from the live path.
        assert "kanban.db" in seen["names"]
        assert "board.json" in seen["names"]
        assert not kb.board_dir("gone").exists()
