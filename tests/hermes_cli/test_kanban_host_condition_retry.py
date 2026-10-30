"""Host-side deaths of an attempt must not spend the card's failure budget.

Incident 2026-10-26: ENOSPC on ``/`` (the profile owner could not create a worker
outbox) and ``runtime_draining`` drain windows killed managed workers before they
did any work; the dispatcher booked every one as a normal crash and blocked six
cards with ``gave_up``. A crash whose output carries one of
``kanban_db_dispatch.HOST_CONDITION_MARKERS`` now re-queues the card, emits
``host_condition_retry`` and leaves ``consecutive_failures`` alone — bounded at
``HOST_CONDITION_RETRY_LIMIT`` per rolling hour, past which ordinary crash
accounting resumes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _crash(conn, tid: str, *, pid: int, claimer_suffix: str = "w"):
    """Claim ``tid``, point it at a dead PID, and let the sweep reap it."""
    host = kb._claimer_id().split(":", 1)[0]
    assert kb.claim_task(conn, tid, claimer=f"{host}:{claimer_suffix}") is not None
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    conn.commit()
    kbd._record_worker_exit(pid, 1 << 8)  # WIFEXITED, rc 1
    return kbd.detect_crashed_workers(conn)


def _events(conn, tid: str, kind: str) -> list[dict]:
    return [
        json.loads(row["payload"] or "{}")
        for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind),
        )
    ]


def test_allowlisted_host_condition_does_not_charge_the_failure_budget(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kbc.connect() as conn:
        for i, marker in enumerate(kbd.HOST_CONDITION_MARKERS):
            tid = kb.create_task(conn, title=f"host {marker}", assignee="a")
            output = f"some agent chatter\nerror: {marker} (mid-line)"
            monkeypatch.setattr(kbd, "_worker_final_output", lambda *_a, _o=output, **_k: _o)

            assert _crash(conn, tid, pid=73000 + i, claimer_suffix=f"w{i}"), (
                "a host-condition death is still booked as a crash event"
            )

            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "ready"
            assert task.consecutive_failures == 0
            spared = _events(conn, tid, "host_condition_retry")
            assert len(spared) == 1
            assert spared[0]["marker"] == marker
            assert spared[0]["pid"] == 73000 + i
            assert spared[0]["attempt"] == 1
            # Dashboard truth is unchanged: the crash itself is still recorded.
            assert _events(conn, tid, "crashed")
            assert tid not in (getattr(kbd.detect_crashed_workers, "_last_auto_blocked", None) or [])


@pytest.mark.parametrize("output", ["Traceback (most recent call last):\nKeyError: 'body'", None])
def test_ordinary_crash_still_counts_and_blocks_at_the_limit(kanban_home, monkeypatch, output):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="genuine crash", assignee="a")
        monkeypatch.setattr(kbd, "_worker_final_output", lambda *_a, **_k: output)

        for hit in range(kbd.DEFAULT_FAILURE_LIMIT):
            _crash(conn, tid, pid=74000 + hit, claimer_suffix=f"g{hit}")
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.consecutive_failures == hit + 1

        assert task is not None and task.status == "blocked"
        assert _events(conn, tid, "gave_up")
        assert _events(conn, tid, "host_condition_retry") == []


def test_host_condition_retries_are_bounded_to_five_per_hour(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="only ever host conditions", assignee="a")
        monkeypatch.setattr(
            kbd, "_worker_final_output", lambda *_a, **_k: "No space left on device",
        )

        for hit in range(kbd.HOST_CONDITION_RETRY_LIMIT):
            _crash(conn, tid, pid=75000 + hit, claimer_suffix=f"h{hit}")
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "ready"
            assert task.consecutive_failures == 0, f"hit {hit} charged the card"

        # The sixth host-condition death falls through to normal accounting.
        _crash(conn, tid, pid=75099, claimer_suffix="h5")

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.consecutive_failures == 1
        assert len(_events(conn, tid, "host_condition_retry")) == kbd.HOST_CONDITION_RETRY_LIMIT
        assert len(_events(conn, tid, "crashed")) == kbd.HOST_CONDITION_RETRY_LIMIT + 1
