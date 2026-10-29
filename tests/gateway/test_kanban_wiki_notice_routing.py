import asyncio
from urllib.parse import parse_qs, urlparse

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


def test_wiki_update_completed_routes_to_tablet_and_advances_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wiki-routing.db"))
    kb.init_db()
    notices = []

    class Response:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def post_to_stub(request, timeout):
        notices.append({
            "method": request.get_method(),
            "payload": parse_qs(urlparse(request.full_url).query),
            "timeout": timeout,
        })
        return Response()

    monkeypatch.setattr("gateway.kanban_wiki_notice.urlopen", post_to_stub)
    task_id, event_id = _completed_subscription("Wiki update: kanban-routing")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert adapter.handled == []
    assert len(notices) == 1
    assert notices[0]["method"] == "POST"
    assert notices[0]["payload"]["kind"] == ["step"]
    assert notices[0]["payload"]["severity"] == ["Information"]
    assert notices[0]["payload"]["title"] == ["Wiki update: kanban-routing"]
    assert "done" in notices[0]["payload"]["text"][0].lower()
    assert _cursor(task_id) == event_id

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(notices) == 1


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
