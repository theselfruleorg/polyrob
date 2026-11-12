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


def _shadow(monkeypatch, holds):
    import core.status_custody as sc
    import core.security.custody_env as ce
    import core.security.host_execution as he
    import core.security.process_hardening as ph
    monkeypatch.setenv('WALLET_SIGNER', 'shadow')
    monkeypatch.setattr(he, 'wallet_custody_enabled', lambda: True)
    monkeypatch.setattr(ph, 'hardening_state', lambda: ph.HardeningResult(ph.STATE_HELD, 'ok'))
    monkeypatch.setattr(ce, 'holds_custody_secret', lambda: holds)
    monkeypatch.setattr(ce, 'custody_loaded', lambda: holds)
    monkeypatch.setattr(ce, 'secrets_in_process_env', lambda: [])
    monkeypatch.setattr(sc, '_shadow_line', lambda sec: 'mock shadow clean')
    monkeypatch.setattr(sc, '_lazy_line', lambda sec: 'mock trusted')
    return sc


def test_shadow_in_a_process_with_no_key_says_so(monkeypatch):
    """116 W6.5: the line describes the process it renders in."""
    sc = _shadow(monkeypatch, holds=False)
    monkeypatch.setattr(sc, '_signer_ping_line', lambda sec: 'mock signer reachable')
    text = "\n".join(sc.custody_section().lines)
    assert "this process holds no key" in text
    assert "still signs locally" not in text and "scrubbed" not in text
    sc = _shadow(monkeypatch, holds=True)
    text = "\n".join(sc.custody_section().lines)
    assert "this process: holds a key · non-dumpable held · env scrubbed" in text


def test_signer_caps_render_through_the_envelope_money_format(monkeypatch):
    import core.signer.client as client
    sc = _shadow(monkeypatch, holds=False)

    class _C:
        def __init__(self, timeout):
            pass

        def call(self, op):
            return {"caps": {"per_tx_usd": 300.0, "daily_usd": 6000.0, "chains": ["base"]}}
    monkeypatch.setattr(client, "SignerClient", _C)
    monkeypatch.setattr(sc, '_cap_drift_health', lambda sec, caps: None)
    text = "\n".join(sc.custody_section().lines)
    assert "hard caps $300.00/tx, $6,000.00/24h" in text and "$6000.0" not in text
