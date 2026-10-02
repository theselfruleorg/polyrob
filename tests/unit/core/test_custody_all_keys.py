import pytest
from core.security.custody_env import CUSTODY_SECRET_ENV

@pytest.mark.parametrize('name', CUSTODY_SECRET_ENV)
def test_every_signing_key_keeps_custody_and_dumpable_health(monkeypatch, name):
    import core.security.custody_env as ce
    import core.security.host_execution as he
    import core.security.process_hardening as ph
    import core.status_custody as sc
    ce._reset_for_tests()
    for item in (*CUSTODY_SECRET_ENV, 'AGENT_WALLET_ENABLED'):
        monkeypatch.delenv(item, raising=False)
    monkeypatch.setenv('WALLET_SIGNER','local')
    monkeypatch.setenv(name,'test-only-key-material'*3)
    monkeypatch.setattr(ph, '_is_linux', lambda: True)
    monkeypatch.setattr(ph, 'hardening_state', lambda: ph.HardeningResult(ph.STATE_FAILED, 'mock failure'))
    monkeypatch.setattr(sc, '_lazy_line', lambda sec: 'mock lazy status')
    try:
        assert he.wallet_custody_enabled(), name
        ce.take_custody_secrets()
        assert he.wallet_custody_enabled(), name
        assert he.host_execution_refusal(), name
        sec=sc.custody_section()
        assert 'custody_dumpable' in {h.key for h in sec.health}, sec
    finally:
        ce._reset_for_tests()
