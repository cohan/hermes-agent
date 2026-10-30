"""Exercise the real poller and sequential worker at deadline expiry."""
from concurrent.futures import Future
from types import SimpleNamespace
import threading
import time

import pytest
from agent import tool_executor as te


def agent():
    return SimpleNamespace(_interrupt_requested=False, _touch_activity=lambda _: None,
                           _tool_worker_threads=set(), _tool_worker_threads_lock=threading.Lock())


@pytest.mark.parametrize("raises", [False, True])
def test_expired_deadline_preserves_settled_future(raises):
    future = Future()
    if raises:
        future.set_exception(ValueError("worker failure"))
    else:
        future.set_result("completed output")
    kwargs = dict(function_name="terminal", deadline=time.monotonic() - 1,
                  started=time.monotonic() - 2,
                  authorization_gate=SimpleNamespace(excluded_seconds=lambda: 0))
    if raises:
        with pytest.raises(ValueError, match="worker failure"):
            te._poll_sequential_future(agent(), future, **kwargs)
    else:
        assert te._poll_sequential_future(agent(), future, **kwargs) == ("done", "completed output")


def test_timeout_reports_unknown_worker_outcome(monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    finished = threading.Event()
    def worker(*args, **kwargs):
        entered.set()
        release.wait(5)
        finished.set()
        return te._ManagedToolResult(result="late output", args={}, middleware_trace=[], blocked=False, dispatched=True)
    monkeypatch.setattr(te, "_run_agent_tool_execution_middleware", worker)
    monkeypatch.setattr(te, "_resolve_sequential_tool_timeout", lambda: 0.05)
    monkeypatch.setattr(te, "_emit_terminal_post_tool_call", lambda *a, **k: None)
    try:
        result = te._run_sequential_tool_execution_middleware(
            agent(), function_name="terminal", function_args={}, effective_task_id="test",
            tool_call_id="deadline-test", execute=lambda _: None)
        assert entered.is_set() and not finished.is_set()
        assert isinstance(result.result, te._ToolTimeoutResult)
        # Honest abandonment: the message must not imply the command failed, and must say the
        # outcome is unknown because the worker was abandoned (not killed).
        assert "may still be running" in result.result
        assert "outcome is not available" in result.result
        assert "abandoned, not killed" in result.result
    finally:
        release.set()
        assert finished.wait(5)
