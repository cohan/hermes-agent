"""A session-turn-lease abort must publish a user-visible notice, once, and must not be
re-ingested as the user's next message.

Diagnosis card `t_30c3e0bc` reproduced both halves of the defect:

* the notice travelled only in ``agent._interrupt_message`` — the HUMAN-INPUT slot — so a later
  interrupt (the terminal batch timeout) could null it before anything was delivered;
* with the notice left in that slot, ``gateway.run_turn._run_agent_drain_pending`` treated it as a
  pending user message and ran a whole phantom turn from it (state.db message 17385, role=user).

The fix latches the notice on the lease at abort time, publishes it through the surface warning
channel, and - with the notice also marked as control flow in ``gateway.run`` - leaves the
human-input slot alone so the drain cannot re-ingest it.
"""
import threading
import time
from types import SimpleNamespace

from agent.turn_facade_lease import (
    LEASE_ABORT_NOTICE_LOST,
    LEASE_ABORT_NOTICE_REFRESH_FAILED,
    DurableTurnLease,
)
from run_agent import AIAgent
from tests.agent.test_cross_process_turn_lease import _DB, _agent_with_db


class _RenewingDB:
    """A lease store that renews successfully: the CONTROL for "a normal turn publishes nothing"."""

    def refresh_session_turn_lease(self, session_id, holder, **kwargs):
        return True


def _agent(**overrides):
    agent = SimpleNamespace(
        session_id="s1",
        _interrupt_requested=False,
        _interrupt_message=None,
        _execution_thread_id=None,
        _turn_liveness_activity_generation=0,
        _last_activity_ts=None,
        warnings=[],
    )
    agent._emit_warning = agent.warnings.append
    agent._liveness_activity_lock = threading.Lock
    interrupts = []
    agent.interrupt = lambda message=None, **kwargs: interrupts.append((message, kwargs))
    agent.interrupt_calls = interrupts
    for key, value in overrides.items():
        setattr(agent, key, value)
    return agent


def _active_lease(agent, db=None):
    lease = DurableTurnLease(
        agent, db if db is not None else SimpleNamespace(),
        "s1", "h", expires_at=time.time() + 300,
    )
    lease.turn_active = True
    return lease


def test_lease_abort_publishes_one_notice_that_a_later_interrupt_cannot_erase():
    agent = _agent()
    lease = _active_lease(agent)

    lease._interrupt_turn(LEASE_ABORT_NOTICE_LOST)
    assert lease.abort_notice == LEASE_ABORT_NOTICE_LOST
    assert agent.interrupt_calls == [
        (LEASE_ABORT_NOTICE_LOST, {"hard_cancel": True, "tool_reason": "session turn lease lost"})
    ]
    assert len(agent.warnings) == 1
    assert LEASE_ABORT_NOTICE_LOST in agent.warnings[0]

    # The terminal batch timeout path nulls the human-input slot on a later, message-less interrupt.
    agent._interrupt_message = None
    assert lease.abort_notice == LEASE_ABORT_NOTICE_LOST
    assert len(agent.warnings) == 1

    # A lease aborts a turn once: a second abort of the same lease publishes nothing further.
    lease._interrupt_turn(LEASE_ABORT_NOTICE_REFRESH_FAILED)
    assert lease.abort_notice == LEASE_ABORT_NOTICE_LOST
    assert len(agent.warnings) == 1


def test_refresh_abort_and_watchdog_commit_publish_once():
    agent = _agent()
    lease = _active_lease(agent)

    # Expired authority: the abort fires before any database call (diagnosis reproduction check 1).
    lease._authority_deadline = time.time() - 1.0
    assert lease.refresh_tick() is False
    assert lease.abort_notice == LEASE_ABORT_NOTICE_REFRESH_FAILED
    assert len(agent.warnings) == 1

    snapshot = SimpleNamespace(generation=0, activity_ts=None, idle_seconds=900.0)
    assert lease.commit_liveness_abort(snapshot, "stalled") is True
    assert lease.abort_notice == LEASE_ABORT_NOTICE_REFRESH_FAILED
    assert len(agent.warnings) == 1


def test_normal_turn_publishes_nothing():
    agent = _agent()
    lease = _active_lease(agent, _RenewingDB())
    assert lease.refresh_tick() is None  # a successful renewal is not an abort
    assert lease.abort_notice is None
    assert agent.warnings == []
    assert agent.interrupt_calls == []

    # And an abort arriving after the lease has been deactivated is ignored, as before.
    lease.stop_refresher()
    lease._interrupt_turn(LEASE_ABORT_NOTICE_LOST)
    assert lease.abort_notice is None
    assert agent.warnings == []


def test_facade_returns_the_latched_notice_on_an_otherwise_empty_aborted_turn(monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)
    agent._session_turn_lease_refresh_interval = 0.01
    agent.warnings = []
    agent._emit_warning = agent.warnings.append
    calls = []

    def track_interrupt(message=None, hard_cancel=False, **kwargs):
        calls.append(message)
        agent._interrupt_requested = True
        agent._interrupt_message = message

    agent.interrupt = track_interrupt
    db.refresh_session_turn_lease = lambda *args, **kwargs: False

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if getattr(_agent, "_interrupt_requested", False):
                return {
                    "final_response": "",
                    "messages": history,
                    "api_calls": 0,
                    "completed": False,
                    "interrupted": True,
                }
            time.sleep(0.01)
        raise AssertionError("refresh loss did not interrupt the turn")

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    result = AIAgent.run_conversation(
        agent, "new message", conversation_history=[{"role": "user", "content": "seed"}]
    )

    # A renewal whose rowcount is 0 is the "lease lost" diagnostic (the row was reclaimed).
    assert result["lease_abort_notice"] == LEASE_ABORT_NOTICE_LOST
    assert result["final_response"] == LEASE_ABORT_NOTICE_LOST
    assert agent.warnings and LEASE_ABORT_NOTICE_LOST in agent.warnings[0]
    assert calls == [LEASE_ABORT_NOTICE_LOST]
