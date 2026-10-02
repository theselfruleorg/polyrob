"""068 G7b: the gate allowing more than the signer will sign is named, with both levers."""
from core.wallet.signer_cap_drift import CapDrift, cap_drift, drift_remedy, drift_text


def test_the_prod_case_is_a_per_tx_drift():
    per_tx, daily = cap_drift({"per_tx_usd": 120.0, "daily_usd": 3000.0}, (300.0, 3000.0))
    assert per_tx.drifted and not daily.drifted
    assert "$300.00" in drift_text(per_tx) and "$120.00" in drift_text(per_tx)
    remedy = drift_remedy(per_tx)
    assert "per_tx_usd = 300" in remedy and "/etc/polyrob/signer.toml" in remedy
    assert "polyrob wallet set-cap per-tx 120" in remedy


def test_a_wider_signer_or_an_unknown_side_is_not_drift():
    assert not CapDrift("per_tx", 500.0, 300.0).drifted
    assert not CapDrift("per_tx", None, 300.0).drifted
    assert not CapDrift("daily", 100.0, None).drifted
    assert not cap_drift({"per_tx_usd": "nan"}, (300.0, None))[0].drifted


def test_custody_status_raises_a_health_item(monkeypatch):
    import core.wallet.signer_cap_drift as mod
    from core.status_custody import _cap_drift_health
    from core.status_snapshot import Section
    monkeypatch.setattr(mod, "gate_caps_now", lambda *a, **k: (300.0, 3000.0))
    sec = Section(name="custody", data={"signer_mode": "shadow"})
    _cap_drift_health(sec, {"per_tx_usd": 120.0, "daily_usd": 3000.0})
    keys = [h.key for h in sec.health]
    assert keys == ["signer_cap_drift_per_tx"]
    assert sec.data["signer_cap_drift"][0]["drifted"] is True


def test_set_cap_warns_when_raised_above_the_signer(monkeypatch):
    import click
    import core.signer as signer_pkg
    import core.signer.client as client_mod
    from cli.commands.wallet import _warn_signer_cap_drift
    out = []
    monkeypatch.setattr(click, "echo", lambda msg="", **k: out.append(str(msg)))
    monkeypatch.setattr(signer_pkg, "signer_mode", lambda: "shadow")

    class _C:
        def __init__(self, **kw):
            pass

        def call(self, op):
            return {"caps": {"per_tx_usd": 120.0, "daily_usd": 3000.0}}
    monkeypatch.setattr(client_mod, "SignerClient", _C)
    _warn_signer_cap_drift("per-tx", 300.0)
    assert any("signer's hard cap is $120.00" in line for line in out)
    out.clear()
    _warn_signer_cap_drift("per-tx", 100.0)
    assert out == []


# ---- Codex B12: disabled is not unknown -------------------------------------

def test_an_unlimited_gate_over_a_finite_signer_is_drift():
    import math
    from core.wallet.signer_cap_drift import cap_drift, drift_remedy, drift_text
    _per_tx, daily = cap_drift({"per_tx_usd": 120, "daily_usd": 100}, (120.0, None))
    assert daily.gate == math.inf and daily.drifted
    assert "UNLIMITED" in drift_text(daily) and "set-cap daily 100" in drift_remedy(daily)


def test_an_unresolved_daily_leg_is_unknown_not_drift():
    from core.wallet.config import _UNRESOLVED
    from core.wallet.signer_cap_drift import cap_drift
    _per_tx, daily = cap_drift({"per_tx_usd": 120, "daily_usd": 100}, (120.0, _UNRESOLVED))
    assert daily.gate is None and not daily.drifted
