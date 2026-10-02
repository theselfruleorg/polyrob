"""Shadow mode retains the P0 health checks because its keys remain local."""
def test_shadow_dumpable_and_resurrected_secret_are_health_alerts(monkeypatch):
    import core.status_custody as sc
    import core.security.custody_env as ce
    import core.security.host_execution as he
    import core.security.process_hardening as ph
    monkeypatch.setenv('WALLET_SIGNER', 'shadow')
    monkeypatch.setattr(he, 'wallet_custody_enabled', lambda: True)
    monkeypatch.setattr(ph, '_is_linux', lambda: True)
    monkeypatch.setattr(ph, 'hardening_state', lambda: ph.HardeningResult(ph.STATE_FAILED, 'mock failed'))
    monkeypatch.setattr(ce, 'holds_custody_secret', lambda: True)
    monkeypatch.setattr(ce, 'custody_loaded', lambda: True)
    monkeypatch.setattr(ce, 'custody_secret', lambda name: 'fake-seed')
    monkeypatch.setattr(ce, 'secrets_in_process_env', lambda: ['AGENT_WALLET_MASTER_SEED'])
    monkeypatch.setattr(sc, '_signer_ping_line', lambda sec: 'mock signer reachable')
    monkeypatch.setattr(sc, '_shadow_line', lambda sec: 'mock shadow clean')
    monkeypatch.setattr(sc, '_lazy_line', lambda sec: 'mock trusted')
    sec = sc.custody_section()
    assert {h.key for h in sec.health} >= {'custody_dumpable', 'custody_env'}, sec
