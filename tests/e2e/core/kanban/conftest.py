"""Board isolation guard for the Kanban e2e suite.

Every test in this directory drives a REAL ``hermes kanban`` child process tree. The only thing
keeping those children off the operator's live board is the environment the harness hands them
(``_helpers.Board.env()``: scratch ``HOME``/``HERMES_HOME``, all ``HERMES_*`` inherited vars dropped).
That is easy to lose — one test that resolves the board from *this* process's environment, or one
harness change that stops stripping ``HERMES_KANBAN_DB``, and the suite writes test fixtures into
``~/.hermes/kanban.db``. That happened: 4001 ``probe`` cards landed on the live board from a test run.

Two things here make that failure loud instead of silent:

* the autouse fixture below points this process's board resolution at a per-test ``tmp_path`` board
  and refuses to run if the resolved path IS the live board;
* ``_helpers.Board.__post_init__`` calls :func:`assert_board_isolated` on the board it builds, so the
  harness itself cannot be pointed at the live board by accident.

Opt out with ``HERMES_E2E_ALLOW_LIVE_KANBAN=1`` — only for a probe that genuinely needs the live
board. Nothing in this directory does today.

Paths are compared lexically (``expanduser`` + ``normpath``), never ``realpath``'d: resolving a path
inside the real Hermes home is itself file I/O against it, which the suite's ``home_io_guard``
rightly refuses.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path
from typing import Iterator

import pytest


def normalise(path: object) -> Path:
    """Absolute, ``~``-expanded, lexically normalised — no filesystem resolution."""
    p = Path(os.path.expanduser(str(path)))
    return Path(os.path.normpath(str(p.absolute())))


# The operator's real Hermes root, from the password database rather than $HOME: the session sandbox
# and the harness both rewrite HOME, and a guard that trusts $HOME is no guard.
REAL_HERMES_ROOT = normalise(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".hermes")

OPT_IN_ENV = "HERMES_E2E_ALLOW_LIVE_KANBAN"

LIVE_BOARDS_DIR = REAL_HERMES_ROOT / "kanban" / "boards"


def _inside_real_root(p: Path) -> bool:
    return p == REAL_HERMES_ROOT or REAL_HERMES_ROOT in p.parents


# Where the live board can live. Captured at import time, before any fixture rewires the env, so a
# pre-test HERMES_KANBAN_DB override (a profile's board) is honoured when it points into the real tree.
_LIVE_BOARD_CANDIDATES = {REAL_HERMES_ROOT / "kanban.db"}
_pre = os.environ.get("HERMES_KANBAN_DB", "").strip()
if _pre:
    _p = normalise(_pre)
    if _inside_real_root(_p):
        _LIVE_BOARD_CANDIDATES.add(_p)

LIVE_BOARD_PATHS = frozenset(_LIVE_BOARD_CANDIDATES)


def live_opt_in() -> bool:
    """True only for an explicit opt-in; anything else means "stay isolated"."""
    return os.environ.get(OPT_IN_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def assert_board_isolated(path: object, *, what: str = "kanban board") -> Path:
    """Return ``path`` normalised, or raise if it is the operator's live board.

    A scratch board under a temp dir *inside* ``~/.hermes`` (pytest's tmp_path lives under the
    profile's cache there) is fine — the deny target is the live board, not the whole tree.
    """
    resolved = normalise(path)
    if resolved in LIVE_BOARD_PATHS or LIVE_BOARDS_DIR in resolved.parents:
        raise RuntimeError(
            f"kanban e2e isolation guard: {what} resolved to the LIVE board database {resolved} "
            f"(real Hermes root {REAL_HERMES_ROOT}). Every test in tests/e2e/core/kanban must run "
            f"against a per-test tmp_path board; refusing to touch the operator's board. Set "
            f"{OPT_IN_ENV}=1 only for a probe that genuinely needs the live board."
        )
    return resolved


def _resolved_board() -> Path:
    """The board this process would open right now, by the documented env precedence."""
    db = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if db:
        return normalise(db)
    home = (os.environ.get("HERMES_KANBAN_HOME", "").strip()
            or os.environ.get("HERMES_HOME", "").strip())
    if home:
        return normalise(Path(home) / "kanban.db")
    return normalise(REAL_HERMES_ROOT / "kanban.db")


@pytest.fixture(autouse=True)
def isolated_board_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path | None]:
    """Point board resolution at a per-test tmp board and fail loudly if it is the live one."""
    if live_opt_in():
        yield None
        return

    board_home = tmp_path / "kanban-home"
    board_home.mkdir(parents=True, exist_ok=True)
    db = board_home / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(board_home))
    assert_board_isolated(_resolved_board())
    yield db
    assert_board_isolated(db)
