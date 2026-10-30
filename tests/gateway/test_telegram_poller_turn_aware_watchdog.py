"""Offline watchdog and real PTB receipt tests.

Preload PTB before gateway/conftest installs its fallback mock:
python -c 'import telegram, pytest; raise SystemExit(pytest.main(["tests/gateway/test_telegram_poller_turn_aware_watchdog.py", "-q"]))'
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telegram

if not isinstance(getattr(telegram, "__version__", None), str):
    pytest.skip("Preload real telegram before pytest (see module docstring)", allow_module_level=True)

from telegram import Update, User
from telegram.ext import Application, TypeHandler
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.update_admission import TelegramApplication


def adapter():
    obj = TelegramAdapter(PlatformConfig(enabled=True, token="111:offline-test"))
    obj._webhook_mode = False
    obj._running = True
    obj._app = MagicMock()
    obj._app.updater.running = True
    obj._app.bot.get_webhook_info = AsyncMock(
        return_value=SimpleNamespace(pending_update_count=1))
    obj._polling_generation_started_monotonic = time.monotonic() - 400
    obj._polling_last_progress_monotonic = time.monotonic() - 400
    return obj


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["handler", "session"])
async def test_pending_and_wire_stall_defer_while_busy(owner):
    obj = adapter()
    task = asyncio.get_running_loop().create_future()
    if owner == "handler":
        obj._inflight_update_ids["111:42"] = object()
    else:
        obj._session_tasks["session"] = task
    with patch.object(obj, "_escalate_stuck_consumer") as escalate, \
            patch.object(obj, "_schedule_polling_recovery") as recover:
        for _ in range(2):
            await obj._probe_pending_updates(obj._app.bot, 5)
            await obj._check_polling_stall()
        escalate.assert_not_called()
        recover.assert_not_called()
        assert obj._polling_pending_stuck_count == 0
    task.set_result(None)
    obj._inflight_update_ids.clear()
    with patch.object(obj, "_schedule_polling_recovery") as recover:
        await obj._check_polling_stall()
        recover.assert_not_called()
        await obj._check_polling_stall()
        recover.assert_called_once()


@pytest.mark.asyncio
async def test_pending_and_wire_stall_still_escalate_when_idle():
    obj = adapter()
    with patch.object(obj, "_escalate_stuck_consumer") as escalate:
        await obj._probe_pending_updates(obj._app.bot, 5)
        escalate.assert_not_called()
        await obj._probe_pending_updates(obj._app.bot, 5)
        escalate.assert_called_once()
    with patch.object(obj, "_schedule_polling_recovery") as recover:
        await obj._check_polling_stall()
        recover.assert_not_called()
        await obj._check_polling_stall()
        recover.assert_called_once()


@pytest.mark.asyncio
async def test_round_trip_resets_stale_probe_evidence():
    obj = adapter()
    with patch.object(obj, "_schedule_polling_recovery") as recover:
        await obj._check_polling_stall()
        obj._polling_progress_accepting = True
        obj._polling_generation = 1
        assert obj._record_polling_progress(1)
        obj._polling_last_progress_monotonic = time.monotonic() - 400
        await obj._check_polling_stall()
        recover.assert_not_called()
        await obj._check_polling_stall()
        recover.assert_called_once()


@pytest.mark.asyncio
async def test_starting_turn_discards_pending_probe_count():
    obj = adapter()
    with patch.object(obj, "_escalate_stuck_consumer") as escalate:
        await obj._probe_pending_updates(obj._app.bot, 5)
        obj._inflight_update_ids["111:42"] = object()
        await obj._probe_pending_updates(obj._app.bot, 5)
        assert obj._polling_pending_stuck_count == 0
        escalate.assert_not_called()


def receipt_app(tmp_path, callback):
    obj = adapter()
    obj._update_receipt_dir = tmp_path
    app = Application.builder().token("111:offline-test").application_class(
        TelegramApplication, {"adapter": obj}).build()
    # Bypass network initialization only; dispatch/admission/persistence are real.
    app.bot._bot_user = User(111, "Offline", True)
    app._initialized = True
    app.add_handler(TypeHandler(Update, callback))
    return obj, app


@pytest.mark.asyncio
async def test_unseen_redelivery_runs_once_then_receipt_dedupes_after_rebuild(tmp_path):
    calls = []

    async def callback(update, context):
        calls.append(update.update_id)

    obj, app = receipt_app(tmp_path, callback)
    # Model an update still unconfirmed server-side at rebuild. This tests
    # admission, not Telegram's network retention.
    await app.process_update(Update(202))
    await obj._update_receipt_flush
    assert calls == [202]
    assert "202" in json.loads((tmp_path / "telegram_update_receipts_111.json").read_text())["update_ids"]
    rebuilt, new_app = receipt_app(tmp_path, callback)
    await new_app.process_update(Update(202))
    assert calls == [202]
    assert "111:202" in rebuilt._seen_update_ids


@pytest.mark.asyncio
async def test_accepted_but_cancelled_work_is_deduped_not_retried(tmp_path):
    """Counterexample to blanket no-loss: admission is not durable completion."""
    entered = asyncio.Event()
    never = asyncio.Event()
    completed = []

    async def callback(update, context):
        entered.set()
        await never.wait()
        completed.append(update.update_id)

    obj, app = receipt_app(tmp_path, callback)
    task = asyncio.create_task(app.process_update(Update(303)))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await obj._update_receipt_flush
    rebuilt, new_app = receipt_app(tmp_path, callback)
    await asyncio.wait_for(new_app.process_update(Update(303)), 1)
    assert completed == []
    assert "111:303" in rebuilt._seen_update_ids
