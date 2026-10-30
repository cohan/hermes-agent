"""A skill pin the assignee cannot load is dropped at creation, not stored.

t_e7d0f1b2 pinned ``gitlab-omnibus-admin`` on a profile that did not have it;
the worker died at startup with ``Unknown skill(s)`` and the dispatcher retried
it into the ground. The pin must be judged against the *assignee's* skill tree
at creation time, dropped if unresolvable, and reported on the card.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        yield kb, conn
    finally:
        conn.close()


def _events(kb, conn, task_id):
    return [e.kind for e in kb.list_events(conn, task_id)]


def _comments(conn, task_id):
    return [r["body"] for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ?", (task_id,))]


def test_loadable_pin_is_kept(board, monkeypatch):
    kb, conn = board
    monkeypatch.setattr(kb, "_assignee_loadable_skills", lambda a: frozenset({"good-skill"}))
    tid = kb.create_task(conn, title="t", assignee="worker", skills=["good-skill"])
    assert kb.get_task(conn, tid).skills == ["good-skill"]
    assert "skill_pin_dropped" not in _events(kb, conn, tid)


def test_unloadable_pin_is_dropped_and_reported(board, monkeypatch):
    kb, conn = board
    monkeypatch.setattr(kb, "_assignee_loadable_skills", lambda a: frozenset({"good-skill"}))
    tid = kb.create_task(conn, title="t", assignee="worker",
                         skills=["good-skill", "missing-skill"], created_by="probe")
    # The resolvable half of the pin survives; only the dead name goes.
    assert kb.get_task(conn, tid).skills == ["good-skill"]
    assert "skill_pin_dropped" in _events(kb, conn, tid)
    drop = [e for e in kb.list_events(conn, tid) if e.kind == "skill_pin_dropped"][-1]
    assert drop.payload["skills"] == ["missing-skill"]
    assert drop.payload["kept"] == ["good-skill"]
    assert any("Skill pin dropped at creation" in body for body in _comments(conn, tid))


def test_all_pins_dead_leaves_no_skills_column(board, monkeypatch):
    kb, conn = board
    monkeypatch.setattr(kb, "_assignee_loadable_skills", lambda a: frozenset())
    tid = kb.create_task(conn, title="t", assignee="worker", skills=["missing-skill"])
    assert not kb.get_task(conn, tid).skills
    assert "skill_pin_dropped" in _events(kb, conn, tid)


def test_unknown_assignee_skill_view_keeps_the_pin(board, monkeypatch):
    """A validator must never rewrite intent when it cannot answer."""
    kb, conn = board
    monkeypatch.setattr(kb, "_assignee_loadable_skills", lambda a: None)
    tid = kb.create_task(conn, title="t", assignee="worker", skills=["whatever"])
    assert kb.get_task(conn, tid).skills == ["whatever"]
    assert "skill_pin_dropped" not in _events(kb, conn, tid)


def test_real_profile_resolution_does_not_explode():
    """``_assignee_loadable_skills`` answers (set or None) without raising for a
    profile that exists and one that does not."""
    from hermes_cli import kanban_db as kb

    assert kb._assignee_loadable_skills("no-such-profile-xyz") is None
    answer = kb._assignee_loadable_skills("ops")
    assert answer is None or isinstance(answer, frozenset)
