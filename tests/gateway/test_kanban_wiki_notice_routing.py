import asyncio
import json

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


def _completed_subscription(title):
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
        assert kb.complete_task(conn, task_id, summary="published")
        event_id = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()["id"]
        return task_id, event_id
    finally:
        conn.close()


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
    import gateway.kanban_wiki_notice as wiki_notice

    notices = []

    def post_to_stub(request, timeout):
        notices.append({
            "method": request.get_method(),
            "content_type": request.get_header("Content-type"),
            "body": request.data,
            "payload": json.loads(request.data.decode("utf-8")),
            "url": request.full_url,
            "timeout": timeout,
        })
        return _Response(body)

    monkeypatch.setattr(wiki_notice, "urlopen", post_to_stub)
    return notices


# --- wire shape: the receiver parses a JSON body, not the query string ---------


def test_post_wiki_notice_sends_json_body_to_the_endpoint(monkeypatch):
    from gateway.kanban_wiki_notice import post_wiki_notice

    notices = _capture_requests(monkeypatch)

    assert post_wiki_notice("Wiki update: shape", "card body text", url="http://panel.test/alert") is True

    assert len(notices) == 1
    sent = notices[0]
    assert sent["method"] == "POST"
    assert sent["url"] == "http://panel.test/alert"
    assert sent["content_type"] == "application/json"
    assert sent["body"], "POST must carry a body: the receiver ignores the query string"
    assert sent["payload"] == {
        "kind": "step",
        "severity": "Information",
        "level": "low",
        "title": "Wiki update: shape",
        "text": "card body text",
    }


def test_post_wiki_notice_returns_false_when_receiver_refuses(monkeypatch):
    from gateway.kanban_wiki_notice import post_wiki_notice

    _capture_requests(monkeypatch, body=b'{"ok": false, "error": "rejected"}')

    assert post_wiki_notice("Wiki update: refused", "text") is False


# --- notifier routing ---------------------------------------------------------


def test_wiki_update_completed_routes_to_tablet_and_advances_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wiki-routing.db"))
    kb.init_db()
    notices = _capture_requests(monkeypatch)
    task_id, event_id = _completed_subscription("Wiki update: kanban-routing")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert adapter.handled == []
    assert len(notices) == 1
    assert notices[0]["method"] == "POST"
    assert notices[0]["content_type"] == "application/json"
    payload = notices[0]["payload"]
    assert payload["kind"] == "step"
    assert payload["severity"] == "Information"
    assert payload["level"] == "low"
    assert payload["title"] == "Wiki update: kanban-routing"
    assert "done" in payload["text"].lower()
    assert _cursor(task_id) == event_id

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(notices) == 1


def test_refused_tablet_notice_falls_back_to_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wiki-fallback.db"))
    kb.init_db()
    # A receiver that refuses (ok=false, or an error status turned into an exception by
    # urlopen) must not consume the event: Telegram was suppressed for this card.
    notices = _capture_requests(monkeypatch, body=b'{"ok": false, "error": "rejected"}')
    task_id, event_id = _completed_subscription("Wiki update: fallback")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(notices) == 1
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert _cursor(task_id) == event_id


def test_raising_tablet_notice_falls_back_to_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wiki-fallback-raise.db"))
    kb.init_db()

    def boom(title, text):
        raise OSError("panel unreachable")

    monkeypatch.setattr("gateway.kanban_wiki_notice.post_wiki_notice", boom)
    _completed_subscription("Wiki update: fallback-raise")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1


def test_disabled_wiki_tablet_routing_uses_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wiki-disabled.db"))
    monkeypatch.setenv("HERMES_WIKI_NOTICE_TO_TABLET", "0")
    kb.init_db()
    notices = []
    monkeypatch.setattr(
        "gateway.kanban_wiki_notice.post_wiki_notice",
        lambda title, text: notices.append({"title": title, "text": text}) or True,
    )
    _completed_subscription("Wiki update: disabled-route")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert notices == []


def test_non_wiki_card_keeps_telegram_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "non-wiki-routing.db"))
    kb.init_db()
    notices = []
    monkeypatch.setattr(
        "gateway.kanban_wiki_notice.post_wiki_notice",
        lambda title, text: notices.append({"title": title, "text": text}) or True,
    )
    _completed_subscription("notify once")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert notices == []
