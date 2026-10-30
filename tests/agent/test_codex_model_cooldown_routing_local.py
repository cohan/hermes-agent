import time
from agent import auxiliary_client as aux
from agent.credential_pool import CredentialPool, PooledCredential


def test_codex_pool_only_benches_requested_model(monkeypatch):
    entry = PooledCredential(id='x', provider='openai-codex', label='test', priority=0, auth_type='oauth', source='manual:device_code', access_token='test-token', refresh_token='test-refresh', model_cooldowns={'blocked-model': time.time()+3600})
    pool = CredentialPool('openai-codex', [entry])
    monkeypatch.setattr(aux, 'load_pool', lambda provider: pool)
    monkeypatch.setattr(aux, '_read_codex_singleton_token', lambda: 'fallback-must-not-bypass')
    monkeypatch.setattr(aux, '_codex_base_url_override', lambda: '')
    assert aux._resolve_codex_credential_and_base('working-model')[0] == 'test-token'
    assert aux._resolve_codex_credential_and_base('blocked-model')[0] is None
