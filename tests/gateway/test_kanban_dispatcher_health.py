"""Dispatcher health warning distinguishes normal concurrency caps from faults."""

from types import SimpleNamespace

from gateway.kanban_watchers import _dispatcher_cap_note, _dispatcher_stuck_warning
from gateway.kanban_watchers_dispatcher import _DispatcherSettings
from hermes_cli.kanban_db_dispatch import DispatchResult


def _settings(*, max_in_progress: int | None = 2, max_in_progress_per_profile: int | None = 1):
    return _DispatcherSettings(
        interval=60.0,
        max_spawn=None,
        max_in_progress=max_in_progress,
        failure_limit=2,
        stale_timeout_seconds=0,
        reconcile_orphans=True,
        default_assignee=None,
        max_in_progress_per_profile=max_in_progress_per_profile,
    )


def test_stuck_warning_names_host_and_profile_caps():
    result = DispatchResult(max_in_progress_reached=2)
    result.skipped_per_profile_capped.append(("task-1", "builder", 1))

    note = _dispatcher_cap_note(_settings(), [("default", result)])
    warning = _dispatcher_stuck_warning(6, "", note)

    assert "max_in_progress=2 reached (2 running)" in warning
    assert "max_in_progress_per_profile=1 reached (builder (1 running))" in warning
    assert "Check profile health (venv, PATH, credentials)" in warning


def test_stuck_warning_keeps_profile_health_wording_without_cap():
    settings = _settings(max_in_progress=None, max_in_progress_per_profile=None)
    note = _dispatcher_cap_note(settings, [("default", SimpleNamespace())])

    warning = _dispatcher_stuck_warning(6, "", note)

    assert note == ""
    assert warning == (
        "kanban dispatcher stuck: ready queue non-empty for 6 consecutive ticks "
        "but 0 workers spawned. Check profile health (venv, PATH, credentials) and "
        "`hermes kanban list --status ready`."
    )
