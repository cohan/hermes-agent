"""Auxiliary provider cooldowns must not be retried into the limit."""

from types import SimpleNamespace

from agent.auxiliary_client import _should_retry_rate_limited_same_key


def _error(*, retry_after=None):
    headers = {} if retry_after is None else {"retry-after": str(retry_after)}
    return SimpleNamespace(status_code=429, response=SimpleNamespace(headers=headers), body=None)


def test_rate_limit_with_retry_after_skips_same_key_probe():
    assert not _should_retry_rate_limited_same_key(_error(retry_after=3316))


def test_rate_limit_without_retry_after_keeps_single_probe():
    assert _should_retry_rate_limited_same_key(_error())
