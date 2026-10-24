"""A 429 that names its own wait must not be retried into.

The vision tool rides the auxiliary path, and the fair-share refusal that prompted this
("retry_after: 3316") was answered by an immediate same-key retry inside the credential rung —
one refusal turned into two attempts, and nothing told the next caller (a cron vision job on a
timer) that the account was in a cooldown at all.
"""
import httpx
import pytest
from openai import RateLimitError
from types import SimpleNamespace

from agent import auxiliary_client as aux
from agent import nous_rate_guard


def _rate_limit(headers=None, body=None) -> RateLimitError:
    return RateLimitError(
        "429 You've reached this model's current fair-share rate limit.",
        response=httpx.Response(
            429, headers=headers or {},
            request=httpx.Request("POST", "https://inference-api.nousresearch.com/v1/chat/completions")),
        body=body or {},
    )


def _route(key: str = "free-jwt") -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(api_key=key), task="vision", tag="", resolved_provider="nous",
        base_info="https://inference-api.nousresearch.com", resolved_model="vision-fixture",
        final_model="vision-fixture", main_runtime=None, resolved_api_key=key,
        resolved_base_url="https://inference-api.nousresearch.com", resolved_api_mode=None,
        route_info=None, async_mode=False, timeout=None,
    )


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "")
    return tmp_path


def test_declared_cooldown_skips_the_same_key_probe(monkeypatch, hermes_home):
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **k: "nous")
    monkeypatch.setattr(
        aux, "_recover_provider_pool",
        lambda *a, **k: pytest.fail("rotated keys inside an account-wide cooldown"))

    error = _rate_limit(body={"error": {"retry_after": 3316}})
    gen = aux._ladder_credential_rungs(error, _route(), {}, True)
    # Any yielded step would be the doomed same-key probe; the rung must fall through instead.
    with pytest.raises(StopIteration) as done:
        next(gen)
    assert done.value.value == (None, error)


def test_cooldown_is_recorded_in_the_shared_breaker(monkeypatch, hermes_home):
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **k: None)
    error = _rate_limit(headers={"retry-after": "3316"})

    gen = aux._ladder_credential_rungs(error, _route(), {}, True)
    with pytest.raises(StopIteration):
        next(gen)

    remaining = nous_rate_guard.nous_rate_limit_remaining()
    assert remaining is not None and remaining > 3000, remaining


def test_429_without_a_declared_wait_still_probes_once(monkeypatch, hermes_home):
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **k: "nous")
    monkeypatch.setattr(aux, "_recover_provider_pool", lambda *a, **k: False)
    error = _rate_limit()

    gen = aux._ladder_credential_rungs(error, _route(), {}, True)
    step = next(gen)
    assert step.kind == "call"
    gen.close()
    assert nous_rate_guard.nous_rate_limit_remaining() is None


def test_short_cooldown_is_honoured_but_not_recorded(monkeypatch, hermes_home):
    """Jitter-sized waits are the transient case the breaker deliberately ignores."""
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **k: "nous")
    monkeypatch.setattr(aux, "_recover_provider_pool", lambda *a, **k: False)
    error = _rate_limit(headers={"retry-after": "5"})

    gen = aux._ladder_credential_rungs(error, _route(), {}, True)
    with pytest.raises(StopIteration):
        next(gen)
    assert nous_rate_guard.nous_rate_limit_remaining() is None


def test_vision_copy_names_the_rate_limit():
    """The tool's failure text says the account is rate-limited, not that the image broke."""
    from tools.vision_tools import _IMAGE_ERROR_RULES

    message = str(_rate_limit(body={"error": {"retry_after": 3316}})).lower()
    template = next(tpl for hints, tpl in _IMAGE_ERROR_RULES if any(h in message for h in hints))
    assert "rate-limited" in template
