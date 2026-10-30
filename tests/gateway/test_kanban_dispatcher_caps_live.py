"""Concurrency caps: the number in config.yaml is the number enforced (t_7ac518e1).

The embedded (gateway) dispatcher resolved ``kanban.*`` settings once at boot and
kept them forever, so a cap edited into ``config.yaml`` while the gateway ran was
never enforced — the file said ``max_in_progress: 1`` while the live dispatcher
still honoured the ``4`` it read at startup, and three workers ran at once on a
hub whose written cap was 1.

``apply_live_caps`` re-resolves the caps every tick (the config load is
mtime-cached), matching ``kanban_db_dispatch.run_daemon`` and the live
auto-decompose toggle. The last test here is the end-to-end shape of the bug:
boot caps, then an edited config, then the board.
"""

from __future__ import annotations

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Every assignee maps to a real profile, so the spawn gate never skips."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


def _settings(max_in_progress=4, per_profile=2):
    """The caps a gateway that booted against ``max_in_progress: 4`` holds."""
    return kwd._DispatcherSettings(
        interval=60.0,
        max_spawn=None,
        max_in_progress=max_in_progress,
        failure_limit=kb.DEFAULT_FAILURE_LIMIT,
        stale_timeout_seconds=0,
        reconcile_orphans=True,
        default_assignee=None,
        max_in_progress_per_profile=per_profile,
    )


def _dispatcher(settings):
    return kwd._KanbanDispatcher(kb, settings)


def _load(kanban_cfg):
    return lambda: {"kanban": kanban_cfg}


def _ready_cards(n, assignee="alice"):
    with kbc.connect() as conn:
        ids = []
        for i in range(n):
            tid = kb.create_task(conn, title=f"card-{i}", assignee=assignee)
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            ids.append(tid)
        conn.commit()
        return ids


def _board(conn):
    return {
        r["status"]: r["n"]
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    }


# ---------------------------------------------------------------------------
# Re-reading the caps
# ---------------------------------------------------------------------------


def test_edited_caps_bind_on_the_next_tick():
    dispatcher = _dispatcher(_settings(4, 2))

    kwd.apply_live_caps(dispatcher, _load({"max_in_progress": 1, "max_in_progress_per_profile": 1}))

    assert dispatcher.settings.max_in_progress == 1
    assert dispatcher.settings.max_in_progress_per_profile == 1


def test_unchanged_caps_log_nothing_every_tick(caplog):
    """A tick runs every 60s: an unchanged cap must not re-log the resolution."""
    dispatcher = _dispatcher(_settings(1, 1))

    with caplog.at_level("INFO"):
        kwd.apply_live_caps(dispatcher, _load({"max_in_progress": 1, "max_in_progress_per_profile": 1}))

    assert "concurrency caps re-read" not in caplog.text


def test_change_is_logged_once(caplog):
    dispatcher = _dispatcher(_settings(4, 2))

    with caplog.at_level("INFO"):
        kwd.apply_live_caps(dispatcher, _load({"max_in_progress": 1}))
        kwd.apply_live_caps(dispatcher, _load({"max_in_progress": 1}))

    assert caplog.text.count("concurrency caps re-read") == 1
    assert "max_in_progress=1 (was 4)" in caplog.text


def test_unset_cap_falls_back_to_the_derived_default():
    """Same rule as boot and the standalone daemon: unset → memory-derived."""
    dispatcher = _dispatcher(_settings(4, 2))

    kwd.apply_live_caps(dispatcher, _load({}))

    assert dispatcher.settings.max_in_progress == kbd.resolve_max_in_progress(None)
    assert dispatcher.settings.max_in_progress_per_profile is None


def test_invalid_caps_are_treated_as_unset():
    dispatcher = _dispatcher(_settings(4, 2))

    kwd.apply_live_caps(
        dispatcher, _load({"max_in_progress": "lots", "max_in_progress_per_profile": 0})
    )

    assert dispatcher.settings.max_in_progress == kbd.resolve_max_in_progress(None)
    assert dispatcher.settings.max_in_progress_per_profile is None


def test_unreadable_config_keeps_the_boot_caps():
    """Fail open on the cap, never drop the limit because a read failed."""
    dispatcher = _dispatcher(_settings(4, 2))

    def boom():
        raise RuntimeError("config gone")

    kwd.apply_live_caps(dispatcher, boom)

    assert dispatcher.settings.max_in_progress == 4
    assert dispatcher.settings.max_in_progress_per_profile == 2


def test_tick_forwards_the_reread_cap_into_dispatch_once(monkeypatch):
    """The refreshed cap — not the boot cap — reaches the tick's dispatch call."""
    captured: dict = {}

    class _FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(
        kbd, "dispatch_once",
        lambda conn, **kwargs: captured.update(kwargs) or kb.DispatchResult(),
    )
    monkeypatch.setattr(kbc, "connect", lambda board=None: _FakeConn())

    dispatcher = _dispatcher(_settings(4, 2))
    kwd.apply_live_caps(dispatcher, _load({"max_in_progress": 1, "max_in_progress_per_profile": 3}))
    dispatcher.tick_once_for_board(kb.DEFAULT_BOARD)

    assert captured["max_in_progress"] == 1
    assert captured["max_in_progress_per_profile"] == 3


# ---------------------------------------------------------------------------
# End to end: the caps actually bind the board
# ---------------------------------------------------------------------------


def test_written_cap_leaves_one_card_ready_for_two_cards(
    monkeypatch, all_assignees_spawnable,
):
    """The card's acceptance shape: cap 1 + 2 ready cards → 1 running, 1 ready."""
    kb.init_db()
    spawns: list[str] = []
    monkeypatch.setattr(
        kbd, "_default_spawn",
        lambda task, workspace, board=None: (spawns.append(task.id), 4242)[1],
    )
    _ready_cards(2)

    with kbc.connect() as conn:
        res = kbd.dispatch_once(conn, max_in_progress=1)
        state = _board(conn)

    assert len(res.spawned) == 1
    assert len(spawns) == 1
    assert state == {"ready": 1, "running": 1}


def test_gateway_tick_honours_an_edited_cap_without_a_restart(
    monkeypatch, all_assignees_spawnable,
):
    """The defect itself: boot caps 4/2, config then edited to 1/1.

    Before ``apply_live_caps`` the tick kept spawning against the boot caps; now
    the edit binds on the next tick, with no gateway restart.
    """
    kb.init_db()
    spawns: list[str] = []
    monkeypatch.setattr(
        kbd, "_default_spawn",
        lambda task, workspace, board=None: (spawns.append(task.id), 4242)[1],
    )
    dispatcher = _dispatcher(_settings(4, 2))
    _ready_cards(3)

    # Tick 1 — still the boot caps: the per-profile cap of 2 is what binds.
    dispatcher.tick_once_for_board(kb.DEFAULT_BOARD)
    with kbc.connect() as conn:
        first = _board(conn)
    assert first == {"ready": 1, "running": 2}

    # Operator edits config.yaml. No restart.
    kwd.apply_live_caps(dispatcher, _load({"max_in_progress": 1, "max_in_progress_per_profile": 1}))

    dispatcher.tick_once_for_board(kb.DEFAULT_BOARD)
    with kbc.connect() as conn:
        second = _board(conn)

    # Cap 1 is already exceeded by the two in-flight workers → nothing new.
    assert second == first
    assert len(spawns) == 2
