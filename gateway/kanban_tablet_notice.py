"""Route Kanban lifecycle notices to the tablet alert receiver.

This is the tablet side of the notifier's routing: the notifier decides *which*
events need Cohan's phone (Telegram) and which are household wallpaper (the
tablet), then hands the tablet-bound ones to :func:`post_notice` after
:func:`build_notice_text` has expanded them and :func:`humanise` has turned the
terse line into a sentence.

The module used to be ``kanban_wiki_notice`` and existed only for cards titled
"Wiki update:". It now serves every lifecycle transition; the wiki title check
survives solely as a label (:func:`wiki_update_task`), never as a routing
decision.
"""

import json
import logging
import os
import re
from typing import Any
from urllib.request import Request, urlopen

logger = logging.getLogger("gateway.kanban")

_DEFAULT_ALERT_URL = "http://10.0.0.113:8791/alert"
_DISABLED_VALUES = {"0", "false", "off"}

# --- Humaniser ---------------------------------------------------------------
# ONE LINE TO CHANGE THE HUMANISER'S MODEL. Cheapest configured non-premium
# model that still writes a decent sentence (nous `auxiliary.kanban_decomposer`
# uses the same slug). Swapping it must not require touching any other line.
NOTICE_MODEL = "deepseek/deepseek-v4.1-flash-1017"
NOTICE_PROVIDER = "nous"
HUMANISE_TIMEOUT = 6.0
HUMANISE_MAX_TOKENS = 90

# The tablet reads at arm's length across a room: ~200 chars, one idea.
NOTICE_MAX_CHARS = 240
TITLE_MAX_CHARS = 60

_HUMANISE_SYSTEM = (
    "You rewrite terse kanban bot notices for a wall-mounted tablet read from across a room. "
    "Return one or two short sentences, under 200 characters, plain everyday words. "
    "Keep the facts that are there: the card, who is on it, what changed, and the outcome. "
    "Never invent details, numbers, ids, file paths, or times. No markdown, no emoji, no quotes, "
    "no preamble — just the sentence(s)."
)

_LOCAL_PATH_RE = re.compile(
    r"(?<![\w:/])(?:/(?:Users|home|private|tmp|var|etc|workspace)/[^\s,;]+|[A-Za-z]:\\[^\s,;]+)"
)
_MARKDOWN_FURNITURE_RE = re.compile(r"[`*_#>\[\]]+")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_TASK_ID_RE = re.compile(r"\bt_[0-9a-z]{6,}\b", re.I)

_STATE_WORDS = {
    "claimed": "running",
    "spawned": "running",
    "completed": "done",
    "blocked": "needs you",
    "block_loop_detected": "triage",
    "review_requested": "in review",
    "changes_requested": "sent back",
    "gave_up": "gave up",
    "crashed": "crashed",
    "timed_out": "timed out",
    "archived": "archived",
}

# --- Colour ------------------------------------------------------------------
# Kanban state -> background, taken from the receiver's own palette
# (URGENCY_COLOURS / LEVEL_URGENCY in alertd.py — do not invent hexes here).
# The receiver forces white text and darkens a background until white reads on
# it, so no `fg` is sent. ONE TABLE TO CHANGE THE COLOUR MAPPING.
STATE_COLOURS = {
    "running": "#3E4A59",   # info    — slate
    "done": "#1B6A6A",      # routine — teal
    "review": "#95581A",    # watch   — amber
    "triage": "#A02A64",    # soon    — plum
    "blocked": "#C4104E",   # now     — raspberry
}
# A state with no mapping takes its level's colour (receiver LEVEL_URGENCY),
# never no colour at all.
LEVEL_COLOURS = {"low": "#3E4A59", "default": "#1B6A6A", "high": "#A02A64"}

_STATUS_STATES = {
    "running": "running",
    "done": "done",
    "complete": "done",
    "completed": "done",
    "review": "review",
    "triage": "triage",
    "blocked": "blocked",
    "archived": "done",
    "unblocked": "running",
}

_EVENT_STATES = {
    "claimed": "running",
    "spawned": "running",
    "completed": "done",
    "archived": "done",
    "unblocked": "running",
    "review_requested": "review",
    "changes_requested": "blocked",
    "blocked": "blocked",
    "block_loop_detected": "triage",
    "gave_up": "blocked",
    "crashed": "blocked",
    "timed_out": "blocked",
}


def notice_state(ev) -> str:
    """The kanban state an event represents, as the colour table names it."""
    kind = str(getattr(ev, "kind", "") or "").strip().lower()
    if kind == "status":
        raw = str((getattr(ev, "payload", None) or {}).get("status") or "").strip().lower()
        return _STATUS_STATES.get(raw, raw or "unknown")
    return _EVENT_STATES.get(kind, kind or "unknown")


def notice_colour(ev, level: str = "low") -> str:
    """Background for this event; an unmapped state falls back to its level colour."""
    colour = STATE_COLOURS.get(notice_state(ev))
    if colour:
        return colour
    return LEVEL_COLOURS.get(str(level or "").strip().lower(), LEVEL_COLOURS["low"])



def wiki_update_task(task) -> bool:
    """Label only: does ``task`` follow the "Wiki update:" title convention?

    Routing no longer consults this — every transition streams to the tablet —
    but the label is still useful for callers that want to say what a card is.
    """
    return bool(task and str(getattr(task, "title", "")).strip().lower().startswith("wiki update:"))


def tablet_enabled() -> bool:
    """Emergency kill switch for the whole tablet notice stream.

    ``HERMES_WIKI_NOTICE_TO_TABLET=0`` predates the general route and is kept
    deliberately: Cohan needs a way to stop the stream without editing code, and
    renaming a live setting is a migration, not a fix.
    """
    return os.environ.get("HERMES_WIKI_NOTICE_TO_TABLET", "").strip().lower() not in _DISABLED_VALUES


# --- Text --------------------------------------------------------------------


def _clean(value, limit: int) -> str:
    """Strip secrets, local paths, ids and markdown from a payload value."""
    text = "" if value is None else str(value)
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:  # pragma: no cover - redaction is best-effort
        pass
    text = _LOCAL_PATH_RE.sub("", text)
    text = _UUID_RE.sub("", text)
    text = _TASK_ID_RE.sub("", text)
    text = _MARKDOWN_FURNITURE_RE.sub("", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _state_word(ev) -> str:
    if ev.kind == "status":
        return str((getattr(ev, "payload", None) or {}).get("status") or "updated").strip().lower()
    return _STATE_WORDS.get(ev.kind, str(ev.kind).replace("_", " "))


def _outcome(ev) -> str:
    """One line of what actually happened, from the event payload."""
    payload = getattr(ev, "payload", None) or {}
    for key in ("summary", "reason", "error", "message"):
        value = payload.get(key)
        if value:
            return _clean(str(value).splitlines()[0], 110)
    return ""


def build_notice_text(task, ev) -> str:
    """Expanded ~200-char notice: card, owner, what changed, one line of outcome."""
    title = _clean(getattr(task, "title", "") or "kanban card", 90)
    assignee = _clean(getattr(task, "assignee", "") or "", 40)
    state = _state_word(ev)
    who = f"{assignee} " if assignee else ""
    text = f"{title} — {who}{state}"
    outcome = _outcome(ev)
    if outcome:
        text = f"{text}. {outcome}"
    if len(text) > NOTICE_MAX_CHARS:
        text = text[: NOTICE_MAX_CHARS - 1].rstrip() + "…"
    return text


def humanise(text: str, *, timeout: float = HUMANISE_TIMEOUT) -> str:
    """Rewrite ``text`` as prose; ``""`` on any failure so the caller sends it plain."""
    if not str(text or "").strip():
        return ""
    try:
        from agent.auxiliary_client import call_llm

        response = call_llm(
            task="kanban_notice",
            provider=NOTICE_PROVIDER,
            model=NOTICE_MODEL,
            messages=[
                {"role": "system", "content": _HUMANISE_SYSTEM},
                {"role": "user", "content": str(text)},
            ],
            temperature=0.3,
            max_tokens=HUMANISE_MAX_TOKENS,
            timeout=timeout,
        )
        reply = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.info("kanban notifier: tablet notice humaniser failed (%s); sending plain text", exc)
        return ""
    reply = " ".join(reply.split())
    if not reply:
        return ""
    if len(reply) > NOTICE_MAX_CHARS:
        reply = reply[: NOTICE_MAX_CHARS - 1].rstrip() + "…"
    return reply


# --- Wire --------------------------------------------------------------------


def post_notice(title, text, *, bg=None, url=None, timeout=5.0) -> bool:
    """Post a silent Information notice, returning False when the panel is unavailable.

    The receiver (panel-alert / alertd.py on copc) parses a JSON body on POST and
    ignores the query string: a body-less POST is read as ``{}`` and rejected with
    HTTP 400, so the payload must be sent as JSON with a Content-Type of
    ``application/json``. ``level`` stays low: routine lifecycle traffic must not
    make the tablet bleep on every ``running``. ``bg`` is the state colour; the
    receiver forces white text and darkens the background for legibility, so no
    ``fg`` is sent.
    """
    endpoint = url or os.environ.get("HERMES_PANEL_ALERT_URL", _DEFAULT_ALERT_URL)
    payload: dict[str, Any] = {
        "kind": "step",
        "severity": "Information",
        "level": "low",
        "title": str(title)[:TITLE_MAX_CHARS],
        "text": str(text),
    }
    if bg:
        payload["bg"] = str(bg)
    try:
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
        return bool(response_payload.get("ok"))
    except Exception as exc:
        logger.warning("kanban notifier: tablet notice failed: %s", exc)
        return False
