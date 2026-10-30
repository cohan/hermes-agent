"""The board-isolation guard itself: it must refuse the live board and pass a tmp board.

Nothing here touches a board database — these are the guard's own tests, so a future edit that
silently disables it (or an opt-in that leaks into normal runs) fails in CI instead of on the
operator's live board. Paths are compared with ``normalise`` rather than ``resolve``: resolving the
live path is file I/O against the real Hermes home and the suite's ``home_io_guard`` refuses it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.core.kanban._helpers import Board
from tests.e2e.core.kanban.conftest import (
    LIVE_BOARD_PATHS,
    OPT_IN_ENV,
    REAL_HERMES_ROOT,
    assert_board_isolated,
    live_opt_in,
    normalise,
)

LIVE_BOARD = sorted(LIVE_BOARD_PATHS)[0]


def test_guard_refuses_the_live_board_path() -> None:
    with pytest.raises(RuntimeError, match="kanban e2e isolation guard"):
        assert_board_isolated(LIVE_BOARD, what="test probe")


def test_env_pointing_at_the_live_board_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact escape shape: a test that resolves the board straight from this process's env."""
    from tests.e2e.core.kanban.conftest import _resolved_board

    monkeypatch.setenv("HERMES_KANBAN_DB", str(LIVE_BOARD))
    assert normalise(_resolved_board()) == normalise(LIVE_BOARD)
    with pytest.raises(RuntimeError, match="LIVE board database"):
        assert_board_isolated(_resolved_board())


def test_opt_in_is_required_and_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OPT_IN_ENV, raising=False)
    assert live_opt_in() is False
    monkeypatch.setenv(OPT_IN_ENV, "1")
    assert live_opt_in() is True


def test_fixture_points_this_process_at_a_tmp_board() -> None:
    from tests.e2e.core.kanban.conftest import _resolved_board

    resolved = normalise(_resolved_board())
    assert resolved not in LIVE_BOARD_PATHS
    assert REAL_HERMES_ROOT / "kanban" / "boards" not in resolved.parents


def test_board_harness_builds_a_board_outside_the_live_one(tmp_path: Path) -> None:
    b = Board(tmp_path, "http://127.0.0.1:9")
    assert normalise(b.db_path) not in LIVE_BOARD_PATHS
    assert str(b.db_path).startswith(str(tmp_path))
