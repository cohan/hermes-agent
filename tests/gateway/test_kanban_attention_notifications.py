"""Attention-only pings keep deliverables, durable cursors and agent wakes."""
import asyncio

import pytest
import yaml

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tests.gateway.test_kanban_notifier import (
    RecordingAdapter,
    _make_runner,
    _run_one_notifier_tick,
)


class ArtifactAdapter(RecordingAdapter):
    extract_local_files = staticmethod(BasePlatformAdapter.extract_local_files)

    def __init__(self):
        super().__init__()
        self.documents = []

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)


def configure(monkeypatch, tmp_path, enabled):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(yaml.safe_dump({"display": {"platforms": {
        "telegram": {"kanban_attention_only": enabled},
    }}}))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))


def event(kind, payload, *, platform="telegram", mode="notify"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="attention gate", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id="chat-1",
                           thread_id="topic-7", delivery_mode=mode)
        kb._append_event(conn, tid, kind=kind, payload=payload)
        return tid, conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    finally:
        conn.close()


def assert_consumed(tid, event_id):
    conn = kbc.connect()
    try:
        row = conn.execute("SELECT last_event_id, last_ping_event_id FROM kanban_notify_subs WHERE task_id=?", (tid,)).fetchone()
        assert row[0] == event_id
        assert row[1] == event_id
    finally:
        conn.close()


@pytest.mark.parametrize("kind,payload", [
    ("completed", {"summary": "done"}),
    ("crashed", {}), ("timed_out", {}), ("gave_up", {}),
    ("blocked", {"kind": "capability"}),
    ("blocked", {"kind": "transient"}),
    ("blocked", {"kind": "dependency"}),
    ("block_loop_detected", {"kind": "capability"}),
    ("status", {"status": "triage"}),
    ("status", {"status": "blocked"}),
])
def test_suppressed_events_advance_cursor_and_do_not_replay(tmp_path, monkeypatch, kind, payload):
    configure(monkeypatch, tmp_path, True)
    kb.init_db()
    tid, eid = event(kind, payload)
    adapter = ArtifactAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert adapter.sent == []
    assert_consumed(tid, eid)
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert adapter.sent == []


@pytest.mark.parametrize("kind,payload", [
    ("review_requested", {"summary": "Review this"}),
    ("changes_requested", {"reason": "Fix this"}),
    ("blocked", {"kind": "needs_input", "reason": "Choose"}),
    ("block_loop_detected", {"kind": "needs_input", "reason": "Choose"}),
])
def test_decisions_still_ping(tmp_path, monkeypatch, kind, payload):
    configure(monkeypatch, tmp_path, True)
    kb.init_db()
    tid, eid = event(kind, payload)
    adapter = ArtifactAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(adapter.sent) == 1
    assert_consumed(tid, eid)


@pytest.mark.parametrize("artifact_source", ["artifacts", "summary"])
def test_completion_uploads_without_text_and_still_wakes(tmp_path, monkeypatch, artifact_source):
    configure(monkeypatch, tmp_path, True)
    kb.init_db()
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4\n%%EOF\n")
    payload = {artifact_source: [str(report)] if artifact_source == "artifacts" else str(report)}
    tid, eid = event("completed", payload, mode="notify+wake")
    adapter = ArtifactAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert adapter.sent == []
    assert len(adapter.documents) == 1
    assert adapter.documents[0]["file_path"] == str(report)
    assert adapter.documents[0]["metadata"]["thread_id"] == "topic-7"
    assert len(adapter.handled) == 1
    assert_consumed(tid, eid)
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(adapter.documents) == 1
    assert len(adapter.handled) == 1


@pytest.mark.parametrize("enabled,platform", [(False, "telegram"), (None, "telegram"), (True, "discord")])
def test_opt_out_default_and_other_platform_keep_completion_ping(tmp_path, monkeypatch, enabled, platform):
    configure(monkeypatch, tmp_path, enabled)
    kb.init_db()
    tid, eid = event("completed", {"summary": "done"}, platform=platform)
    adapter = ArtifactAdapter()
    runner = _make_runner(adapter)
    runner.adapters = {Platform(platform): adapter}
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert_consumed(tid, eid)
