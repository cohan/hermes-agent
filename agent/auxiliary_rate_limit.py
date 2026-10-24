"""The auxiliary path's 429 policy: honour a declared cooldown, and tell the next caller.

Backoff contract for every auxiliary call (``vision_analyze`` rides this path, so does the
gateway's screenshot pre-analysis and the text fallback for non-vision models):

* A 429 that names its own wait (``Retry-After`` header or ``retry_after`` body field) is a
  cooldown, not a blip. The caller must not re-ask the same key inside it: it waits the declared
  time, fails fast and says so, or leaves for another provider. It never retries immediately.
* A cooldown long enough to be an exhausted allowance rather than jitter is written to the shared
  ``rate_limits/nous.json`` breaker, so later calls — a cron job on a timer, the next turn, the
  next profile process — skip Nous instead of each spending a doomed attempt against a window that
  is already spent.
* A Nous cooldown is account-wide: every key in that pool draws on the same fair-share window, so
  pool rotation inside it is just another attempt spent against it and is skipped too.
* A 429 with no declared wait keeps the old behaviour, one bounded same-key probe: that shape is
  usually one upstream model briefly out of capacity, not this account's allowance.

The limit itself is per account, not per profile (the refusal comes from the inference host that
fronts one Nous account), while the breaker file is per ``HERMES_HOME``. That is why the fix is
patience inside a profile and this shared file wherever profiles share a home; profiles with
separate homes each back off independently.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A declared cooldown at or above this is an exhausted allowance rather than jitter, so it is worth
# writing to the cross-session breaker: the same floor ``nous_rate_guard`` uses for a bucket it
# considers genuinely exhausted.
COOLDOWN_RECORDED_MIN_S = 60.0


def credential_rung_accepts(exc: Exception) -> bool:
    """Whether the credential rung owns this failure: a dead key (auth), a dead wallet (payment),
    or a spent allowance (rate limit). A connection failure is deliberately not this — a fresh key
    cannot fix an unreachable endpoint."""
    from agent.auxiliary_client import _is_auth_error, _is_payment_error, _is_rate_limit_error

    return _is_auth_error(exc) or _is_payment_error(exc) or _is_rate_limit_error(exc)


def rate_limit_verdict(
    error: Exception, *, task: Optional[str], tag: str, client: Any,
    resolved_api_key: Optional[str], client_is_nous: bool,
) -> tuple[bool, Optional[float], bool]:
    """``(rate_limited, cooldown_seconds, nous_cooling)`` for one auxiliary failure.

    ``rate_limited`` gates the credential rung's same-key probe, ``cooldown`` is the wait the
    provider asked for (None when it named none), and ``nous_cooling`` is the account-wide case
    where rotation is pointless too.
    """
    from agent.auxiliary_client import _is_payment_error, _is_rate_limit_error

    rate_limited = _is_rate_limit_error(error) and not _is_payment_error(error)
    if not rate_limited:
        return False, None, False
    cooldown = honour_declared_cooldown(
        error, task=task, tag=tag, client=client,
        resolved_api_key=resolved_api_key, client_is_nous=client_is_nous)
    return True, cooldown, cooldown is not None and client_is_nous


def honour_declared_cooldown(
    error: Exception, *, task: Optional[str], tag: str, client: Any,
    resolved_api_key: Optional[str], client_is_nous: bool,
) -> Optional[float]:
    """Seconds the provider itself asked us to wait, recorded in the breaker when long enough.

    Returns None when the error carries no usable wait, leaving the caller's ordinary one-shot
    retry in play.
    """
    from agent.retry_utils import provider_retry_after_seconds

    wait = provider_retry_after_seconds(error)
    if wait is None or not client_is_nous or wait < COOLDOWN_RECORDED_MIN_S:
        return wait
    try:
        from agent.nous_rate_guard import record_nous_rate_limit
        from hermes_cli.anon_auth import is_anonymous_request

        key = resolved_api_key or getattr(client, "api_key", "")
        record_nous_rate_limit(
            headers=getattr(getattr(error, "response", None), "headers", None),
            error_context={"reset_at": time.time() + wait},
            anonymous=is_anonymous_request("nous", key),
        )
        logger.info(
            "Auxiliary %s%s: provider asked for a %.0fs wait; recorded the cooldown so later "
            "calls back off instead of retrying into it", task or "call", tag, wait)
    except Exception:  # pragma: no cover - the breaker is an optimisation, never a failure path
        logger.debug("Auxiliary: could not record the Nous cooldown", exc_info=True)
    return wait
