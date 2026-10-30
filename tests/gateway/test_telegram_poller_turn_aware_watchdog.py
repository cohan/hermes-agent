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
async def test_accepted_then_cancelled_work_is_retried_after_rebuild(tmp_path):
    """Admission is not durable completion: a cancelled turn is re-run, not deduped.

    Regression guard for the gap disclosed by t_302b5652 and closed by t_300a9e1e: an
    update admitted (``claim.accepted``) and then cancelled by a full gateway teardown used
    to be recorded in the durable receipt set anyway, so a restarted gateway dropped
    Telegram's redelivery and the work was lost with no retry. It must instead be recorded
    as incomplete and re-run.
    """
    entered = asyncio.Event()
    never = asyncio.Event()
    calls = []

    async def callback(update, context):
        calls.append(update.update_id)
        if len(calls) == 1:
            entered.set()
            await never.wait()

    obj, app = receipt_app(tmp_path, callback)
    task = asyncio.create_task(app.process_update(Update(303)))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    flush = obj._update_receipt_flush
    if flush is not None:
        await flush
    assert calls == [303]
    # Admitted, then cancelled, is recorded as incomplete -- never as completed history.
    payload = json.loads((tmp_path / "telegram_update_receipts_111.json").read_text())
    assert payload["update_ids"] == {}
    assert "303" in payload["incomplete_ids"]
    # Telegram redelivers the unacked update to a rebuilt adapter: it re-runs, not dropped.
    rebuilt, new_app = receipt_app(tmp_path, callback)
    assert "111:303" not in rebuilt._seen_update_ids
    await asyncio.wait_for(new_app.process_update(Update(303)), 1)
    assert calls == [303, 303]
