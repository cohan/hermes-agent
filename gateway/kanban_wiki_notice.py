"""Route wiki-update Kanban notices to the tablet alert receiver."""

import json
import logging
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen


logger = logging.getLogger("gateway.kanban")
_DEFAULT_ALERT_URL = "http://10.0.0.113:8791/alert"
_DISABLED_VALUES = {"0", "false", "off"}


def wiki_update_task(task) -> bool:
    """Return whether ``task`` has the wiki-update title convention."""
    return bool(task and str(getattr(task, "title", "")).strip().lower().startswith("wiki update:"))


def tablet_enabled() -> bool:
    """Return whether wiki-update notices should bypass Telegram."""
    return os.environ.get("HERMES_WIKI_NOTICE_TO_TABLET", "").strip().lower() not in _DISABLED_VALUES


def post_wiki_notice(title, text, *, url=None, timeout=5.0) -> bool:
    """Post a silent Information notice, returning False when the panel is unavailable."""
    endpoint = url or os.environ.get("HERMES_PANEL_ALERT_URL", _DEFAULT_ALERT_URL)
    payload = {
        "kind": "step",
        "severity": "Information",
        "level": "low",
        "title": str(title)[:60],
        "text": str(text),
    }
    try:
        request = Request(
            f"{endpoint}?{urlencode(payload)}",
            data=b"",
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
        return bool(response_payload.get("ok"))
    except Exception as exc:
        logger.warning("kanban notifier: tablet wiki notice failed: %s", exc)
        return False
