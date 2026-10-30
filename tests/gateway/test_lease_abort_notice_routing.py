"""A session-turn-lease abort diagnostic must not be re-ingested as the user's next message.

Diagnosis card `t_30c3e0bc` reproduced the routing defect: the lease notice travelled in the
human-input slot (``_interrupt_message``), so ``_run_agent_drain_pending`` requeued it and a whole
phantom turn ran from it (state.db message 17385, role=user). ``_is_control_interrupt_message`` is
the single predicate that decides requeue, so the lease diagnostics are asserted to be control flow
there - and ordinary user text is asserted not to be.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agent.turn_facade_lease import LEASE_ABORT_NOTICES
from gateway.run import _is_control_interrupt_message


@pytest.mark.parametrize("notice", sorted(LEASE_ABORT_NOTICES))
def test_lease_abort_diagnostics_are_control_flow(notice):
    assert _is_control_interrupt_message(notice) is True
    # The gateway normalises whitespace and case before comparing.
    assert _is_control_interrupt_message("  " + notice.upper().replace(" ", "  ") + "  ") is True


@pytest.mark.parametrize(
    "message",
    ["what is the capital of France?", "Session turn lease", "", None],
)
def test_ordinary_messages_are_not_control_flow(message):
    assert _is_control_interrupt_message(message) is False


def test_drain_does_not_requeue_the_lease_abort_notice_as_a_user_turn():
    """The drain leaves the slot alone for a lease notice; a real queued message still requeues."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._draining = False
    runner._promote_queued_event = lambda key, adapter, event: event
    adapter = SimpleNamespace(_pending_messages={}, get_pending_message=lambda key: None)
    result = {
        "interrupted": True,
        "final_response": "",
        "interrupt_message": min(LEASE_ABORT_NOTICES),
    }
    event, pending = asyncio.run(
        GatewayRunner._run_agent_drain_pending(
            runner, result, adapter, SimpleNamespace(), "session-key"
        )
    )
    assert event is None and pending is None

    result["interrupt_message"] = "please also check the backups"
    event, pending = asyncio.run(
        GatewayRunner._run_agent_drain_pending(
            runner, result, adapter, SimpleNamespace(), "session-key"
        )
    )
    assert event is None and pending == "please also check the backups"
