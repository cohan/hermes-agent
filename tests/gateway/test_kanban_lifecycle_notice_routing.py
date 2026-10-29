"""Routing for Kanban lifecycle notices: tablet for routine, Telegram for needs-you.

Cohan's rule (2026-10-29): every status transition reaches the tablet, humanised
and expanded; blocked/needs-you and triage escalations stay on Telegram; exactly
one delivery per transition, and a refused tablet POST falls back to Telegram
rather than losing the event.
"""

import asyncio
import json

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


class _Response:
    def __init__(self, body=b'{"ok": true}'):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture(autouse=True)
def _reset_tablet_sent(monkeypatch):
    """The sent-marker is process-wide; tests must not inherit each other's ids."""
    from gateway import kanban_watchers_notifier as notifier

    monkeypatch.setattr(notifier, "_TABLET_SENT", {})


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _subscribed_task(title="notify once"):
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title=title, assignee="support")
        kbn.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            delivery_mode="notify+wake",
        )
        return task_id
    finally:
        conn.close()


def _completed_subscription(title="notify once", summary="published"):
    task_id = _subscribed_task(title)
    conn = kbc.connect()
    try:
        assert kb.complete_task(conn, task_id, summary=summary)
        return task_id, _last_event_id(conn, task_id, "completed")
    finally:
        conn.close()


def _append(task_id, kind, payload):
    """Append one lifecycle event the way the kernel does."""
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, kind, payload)
        return _last_event_id(conn, task_id, kind)
    finally:
        conn.close()


def _last_event_id(conn, task_id, kind):
    return conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()["id"]


def _cursor(task_id):
    conn = kbc.connect()
    try:
        return conn.execute(
            "SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ?", (task_id,)
        ).fetchone()["last_event_id"]
    finally:
        conn.close()


def _capture_requests(monkeypatch, *, body=b'{"ok": true}'):
    """Point ``urlopen`` at a stub that records the wire shape of each request."""
    from gateway import kanban_tablet_notice as tablet_notice

    notices = []

    def post_to_stub(request, timeout):
        notices.append({
            "method": request.get_method(),
            "content_type": request.get_header("Content-type"),
            "payload": json.loads(request.data.decode("utf-8")),
            "url": request.full_url,
        })
        return _Response(body)

    monkeypatch.setattr(tablet_notice, "urlopen", post_to_stub)
    return notices


def _capture_humaniser(monkeypatch, *, prose="The router now sends every notice to the tablet."):
    """Record every humaniser call and answer with fixed prose."""
    from gateway import kanban_tablet_notice as tablet_notice

    calls = []

    def fake_humanise(text, **kwargs):
        calls.append(text)
        return prose

    monkeypatch.setattr(tablet_notice, "humanise", fake_humanise)
    return calls


# --- wire shape: the receiver parses a JSON body, not the query string ---------


def test_post_notice_sends_json_body_to_the_endpoint(monkeypatch):
    from gateway.kanban_tablet_notice import post_notice

    notices = _capture_requests(monkeypatch)

    assert post_notice("Card title", "card body text", url="http://panel.test/alert") is True

    assert len(notices) == 1
    sent = notices[0]
    assert sent["method"] == "POST"
    assert sent["url"] == "http://panel.test/alert"
    assert sent["content_type"] == "application/json"
    assert sent["payload"] == {
        "kind": "step",
        "severity": "Information",
        "level": "low",
        "title": "Card title",
        "text": "card body text",
    }


def test_post_notice_returns_false_when_receiver_refuses(monkeypatch):
    from gateway.kanban_tablet_notice import post_notice

    _capture_requests(monkeypatch, body=b'{"ok": false, "error": "rejected"}')

    assert post_notice("Card title", "text") is False


# --- expanded text ------------------------------------------------------------


def test_notice_text_carries_card_owner_state_and_outcome():
    from gateway.kanban_tablet_notice import build_notice_text

    class _Ev:
        kind = "completed"
        payload = {"summary": "Moved the page to DokuWiki and read it back."}

    class _Task:
        title = "Wiki update: alertd contract"
        assignee = "support"

    text = build_notice_text(_Task(), _Ev())

    assert text.startswith("Wiki update: alertd contract")
    assert "support" in text
    assert "done" in text.lower()
    assert "read it back" in text
    assert len(text) <= 240


def test_notice_text_strips_ids_and_paths():
    from gateway.kanban_tablet_notice import build_notice_text

    class _Ev:
        kind = "status"
        payload = {"status": "running", "summary": "editing /home/cohan/.hermes/hermes-agent/x.py t_7cbb9477"}

    class _Task:
        title = "Fix the notifier"
        assignee = "builder"

    text = build_notice_text(_Task(), _Ev())

    assert "/home/cohan" not in text
    assert "t_7cbb9477" not in text


# --- routing: tablet for routine transitions ----------------------------------


def test_running_transition_reaches_tablet_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "running.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch)
    _capture_humaniser(monkeypatch)
    task_id = _subscribed_task("prep the media shelf")
    _append(task_id, "spawned", {"pid": 4242, "started_at": "x"})

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert len(notices) == 1
    assert notices[0]["payload"]["level"] == "low"


def test_completed_transition_reaches_tablet_and_advances_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "done.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch)
    _capture_humaniser(monkeypatch, prose="The media shelf is ready to sit on.")
    task_id, event_id = _completed_subscription("build the media shelf")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert len(notices) == 1
    assert notices[0]["payload"]["text"] == "The media shelf is ready to sit on."
    assert notices[0]["payload"]["title"] == "build the media shelf"
    assert _cursor(task_id) == event_id

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(notices) == 1


def test_review_requested_reaches_tablet(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch)
    calls = _capture_humaniser(monkeypatch)
    task_id = _subscribed_task("wire the shelf light")

    conn = kbc.connect()
    try:
        assert kb.request_review(conn, task_id, summary="light wired, needs a look")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert len(notices) == 1
    assert "review" in calls[0].lower()


# --- routing: Telegram for needs-you ------------------------------------------


def test_blocked_needs_input_goes_to_telegram_not_tablet(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "blocked.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch)
    _capture_humaniser(monkeypatch)
    task_id = _subscribed_task("order the shelf brackets")

    conn = kbc.connect()
    try:
        assert kb.block_task(conn, task_id, reason="which bracket size?", kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert notices == []
    assert len(adapter.sent) == 1
    assert "bracket" in adapter.sent[0]["text"]


def test_triage_status_goes_to_telegram_not_tablet(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "triage.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch)
    _capture_humaniser(monkeypatch)
    task_id = _subscribed_task("flaky shelf job")
    _append(task_id, "status", {"status": "triage", "reason": "re-blocked for the same cause"})

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert notices == []
    assert len(adapter.sent) == 1


# --- humaniser ----------------------------------------------------------------


def test_humaniser_called_once_per_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "humanise.db"))
    kb.init_db()
    _capture_requests(monkeypatch)
    calls = _capture_humaniser(monkeypatch)
    _completed_subscription("build the shelf")

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(RecordingAdapter())))

    assert len(calls) == 1
    assert "build the shelf" in calls[0]


def test_humaniser_failure_sends_plain_expanded_text(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "humanise-fail.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch)

    from gateway import kanban_tablet_notice as tablet_notice

    def boom(text, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(tablet_notice, "humanise", boom)
    _completed_subscription("build the shelf", summary="shelf built and loaded")

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(RecordingAdapter())))

    assert len(notices) == 1
    text = notices[0]["payload"]["text"]
    assert "build the shelf" in text
    assert "done" in text.lower()
    assert "shelf built and loaded" in text


# --- fallback and duplication -------------------------------------------------


def test_refused_tablet_notice_falls_back_to_telegram_exactly_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "fallback.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch, body=b'{"ok": false, "error": "rejected"}')
    _capture_humaniser(monkeypatch)
    task_id, event_id = _completed_subscription("build the shelf")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(notices) == 1
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert _cursor(task_id) == event_id


def test_raising_tablet_notice_falls_back_to_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "fallback-raise.db"))
    kb.init_db()

    from gateway import kanban_tablet_notice as tablet_notice

    def boom(title, text, **kwargs):
        raise OSError("panel unreachable")

    monkeypatch.setattr(tablet_notice, "post_notice", boom)
    _completed_subscription("build the shelf")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1


def test_wiki_update_card_produces_one_notice_not_two(tmp_path, monkeypatch):
    """The wiki special case must be a subset of the general path, not a sibling."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wiki.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch)
    calls = _capture_humaniser(monkeypatch)
    _completed_subscription("Wiki update: alertd contract")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(notices) == 1
    assert len(calls) == 1
    assert adapter.sent == []


def test_two_subscriptions_still_post_one_tablet_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "two-subs.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch)
    _capture_humaniser(monkeypatch)
    task_id, _ = _completed_subscription("build the shelf")
    conn = kbc.connect()
    try:
        kbn.add_notify_sub(
            conn, task_id=task_id, platform="telegram", chat_id="chat-2", delivery_mode="notify+wake",
        )
    finally:
        conn.close()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(RecordingAdapter())))

    assert len(notices) == 1


# --- colour -------------------------------------------------------------------


@pytest.mark.parametrize(("ev", "colour"), [
    (("spawned", {"pid": 1}), "#3E4A59"),
    (("completed", {"summary": "done"}), "#1B6A6A"),
    (("review_requested", {"summary": "look"}), "#95581A"),
    (("block_loop_detected", {"reason": "same cause"}), "#A02A64"),
    (("blocked", {"kind": "needs_input"}), "#C4104E"),
    (("status", {"status": "running"}), "#3E4A59"),
    (("status", {"status": "triage"}), "#A02A64"),
])
def test_state_colours_come_from_the_receiver_palette(ev, colour):
    from gateway.kanban_tablet_notice import notice_colour

    class _Ev:
        kind, payload = ev

    assert notice_colour(_Ev()) == colour


def test_unknown_state_falls_back_to_the_level_colour():
    from gateway.kanban_tablet_notice import notice_colour

    class _Ev:
        kind = "status"
        payload = {"status": "quarantined"}

    assert notice_colour(_Ev()) == "#3E4A59"
    assert notice_colour(_Ev(), level="default") == "#1B6A6A"
    assert notice_colour(_Ev(), level="high") == "#A02A64"


def test_running_notice_carries_its_colour_on_the_wire(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "colour.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch)
    _capture_humaniser(monkeypatch)
    task_id = _subscribed_task("prep the media shelf")
    _append(task_id, "spawned", {"pid": 4242, "started_at": "x"})

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(RecordingAdapter())))

    assert notices[0]["payload"]["bg"] == "#3E4A59"
    assert "fg" not in notices[0]["payload"]


def test_colour_survives_the_refusal_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "colour-refused.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch, body=b'{"ok": false, "error": "rejected"}')
    _capture_humaniser(monkeypatch)
    _completed_subscription("build the shelf")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert notices[0]["payload"]["bg"] == "#1B6A6A"
    assert len(adapter.sent) == 1


# --- kill switch --------------------------------------------------------------


def test_kill_switch_keeps_the_whole_stream_on_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "disabled.db"))
    monkeypatch.setenv("HERMES_WIKI_NOTICE_TO_TABLET", "0")
    kb.init_db()
    notices = _capture_requests(monkeypatch)
    _capture_humaniser(monkeypatch)
    _completed_subscription("Wiki update: disabled-route")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert notices == []
