"""The signer's hard caps are the envelope (owner decision 2026-10-06): the gate
enforces min(configured, signer), so the two can no longer disagree."""
import math

import pytest

import core.signer as signer_pkg
import core.signer.client as client_mod
from core.wallet import signer_envelope as env_mod
from core.wallet.config import (configured_caps_now, effective_daily_cap_usd,
                                effective_max_per_tx_usd)
from core.wallet.signer_envelope import BoundLeg, bound_legs, bound_text


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.delenv(env_mod.SIGNER_PROCESS_ENV, raising=False)
    env_mod.reset_cache()
    yield
    env_mod.reset_cache()


def _signer(monkeypatch, *, mode="shadow", caps=None, fail=False):
    calls = []
    monkeypatch.setattr(signer_pkg, "signer_mode", lambda: mode)

    class _C:
        def __init__(self, **kw):
            pass

        def call(self, op):
            calls.append(op)
            if fail:
                raise client_mod.SignerUnavailable("down")
            return {"caps": caps or {}}
    monkeypatch.setattr(client_mod, "SignerClient", _C)
    return calls


# The prod case (2026-10-06): env per-tx 120 + owner pref 3000, env daily 7000;
# signer.toml per_tx 120 / daily 3000.
PROD_ENV = {"AGENT_WALLET_MAX_PER_TX_USD": "120", "WALLET_DAILY_CAP_USD": "7000"}
PROD_SIGNER = {"per_tx_usd": 120.0, "daily_usd": 3000.0}


def _pref(tmp_path, monkeypatch):
    from core import prefs
    monkeypatch.setattr(prefs, "resolve", lambda key, *a, env_value=None, default=None, **k:
                        3000.0 if key == "budget.wallet_per_tx_usd" else env_value)


def test_the_gate_enforces_the_signer_envelope(tmp_path, monkeypatch):
    _pref(tmp_path, monkeypatch)
    _signer(monkeypatch, caps=PROD_SIGNER)
    assert effective_max_per_tx_usd("u1", tmp_path, env=PROD_ENV) == 120.0
    assert effective_daily_cap_usd("u1", tmp_path, env=PROD_ENV) == 3000.0
    # the configured values are still what the owner set
    assert configured_caps_now(PROD_ENV, user_id="u1", home_dir=tmp_path) == (3000.0, 7000.0)


def test_local_mode_never_asks_the_signer(tmp_path, monkeypatch):
    _pref(tmp_path, monkeypatch)
    calls = _signer(monkeypatch, mode="local", caps=PROD_SIGNER)
    assert effective_max_per_tx_usd("u1", tmp_path, env=PROD_ENV) == 3000.0
    assert calls == []


def test_inside_the_signer_process_no_self_ping(tmp_path, monkeypatch):
    _pref(tmp_path, monkeypatch)
    calls = _signer(monkeypatch, caps=PROD_SIGNER)
    monkeypatch.setenv(env_mod.SIGNER_PROCESS_ENV, "1")
    assert effective_max_per_tx_usd("u1", tmp_path, env=PROD_ENV) == 3000.0
    assert calls == []


def test_an_unanswering_signer_clamps_to_zero_until_first_confirmation(tmp_path, monkeypatch):
    _pref(tmp_path, monkeypatch)
    _signer(monkeypatch, fail=True)
    assert effective_max_per_tx_usd("u1", tmp_path, env=PROD_ENV) == 0.0
    assert effective_daily_cap_usd("u1", tmp_path, env=PROD_ENV) == 0.0


def test_a_wider_signer_changes_nothing(tmp_path, monkeypatch):
    _pref(tmp_path, monkeypatch)
    _signer(monkeypatch, caps={"per_tx_usd": 5000.0, "daily_usd": 9000.0})
    assert effective_max_per_tx_usd("u1", tmp_path, env=PROD_ENV) == 3000.0
    assert effective_daily_cap_usd("u1", tmp_path, env=PROD_ENV) == 7000.0


def test_a_disabled_daily_takes_the_signer_cap(tmp_path, monkeypatch):
    _signer(monkeypatch, caps=PROD_SIGNER)
    assert effective_daily_cap_usd("u1", tmp_path, env={"WALLET_DAILY_CAP_USD": "none"}) == 3000.0


def test_the_ping_is_cached(tmp_path, monkeypatch):
    calls = _signer(monkeypatch, caps=PROD_SIGNER)
    for _ in range(5):
        effective_max_per_tx_usd("u1", tmp_path, env=PROD_ENV)
    assert calls == ["ping"]


def test_bound_legs_name_the_binding_cap_and_where_to_raise_it():
    per_tx, daily = bound_legs(PROD_SIGNER, (3000.0, 7000.0))
    assert (per_tx.leg, per_tx.configured, per_tx.signer) == ("per_tx", 3000.0, 120.0)
    assert daily.leg == "daily"
    text = bound_text(per_tx)
    assert "$120.00" in text and "$3,000.00" in text
    assert "per_tx_usd" in text and "/etc/polyrob/signer.toml" in text
    assert bound_legs(PROD_SIGNER, (100.0, 3000.0)) == []
    assert bound_legs({"per_tx_usd": "nan"}, (300.0, 100.0)) == []
    (unlimited,) = bound_legs({"per_tx_usd": 500, "daily_usd": 100}, (100.0, None))
    assert unlimited.configured == math.inf and "unlimited" in bound_text(unlimited)


def test_custody_status_is_a_line_not_a_repeating_health_item(tmp_path, monkeypatch):
    import core.wallet.config as cfg
    from core.status_custody import _cap_drift_health
    from core.status_snapshot import Section
    monkeypatch.setattr(cfg, "configured_caps_now", lambda *a, **k: (3000.0, 7000.0))
    sec = Section(name="custody", data={"signer_mode": "shadow"})
    _cap_drift_health(sec, PROD_SIGNER)
    assert sec.health == []
    assert [b["leg"] for b in sec.data["signer_bound_caps"]] == ["per_tx", "daily"]
    assert any("bound by the signer" in line for line in sec.lines)


def test_set_cap_says_the_signer_stopped_the_raise(tmp_path, monkeypatch):
    import core.wallet.config as cfg
    from cli.commands.wallet import _signer_bound
    _signer(monkeypatch, caps=PROD_SIGNER)
    monkeypatch.setattr(cfg, "configured_caps_now", lambda *a, **k: (3000.0, 7000.0))
    b = _signer_bound("per-tx", "u1", tmp_path)
    assert isinstance(b, BoundLeg) and b.signer == 120.0
    monkeypatch.setattr(cfg, "configured_caps_now", lambda *a, **k: (100.0, 2000.0))
    assert _signer_bound("per-tx", "u1", tmp_path) is None


# ---- every hard leg the signer enforces is a clamp on the agent side --------

X402_SIGNER = dict(PROD_SIGNER, x402_per_payment_usd=0.5, x402_max_window_sec=300)


def test_the_x402_autonomous_ceiling_is_clamped_to_the_signer(monkeypatch):
    from core.config_policy.spend_lane import x402_autonomous_ceiling_usd
    _signer(monkeypatch, caps=X402_SIGNER)
    monkeypatch.setenv("X402_AUTONOMOUS_MAX_USD", "2.0")
    assert x402_autonomous_ceiling_usd() == 0.5


def test_the_x402_window_is_the_signers_when_lower(monkeypatch):
    from tools.x402.real_client import _max_authorization_seconds
    assert _max_authorization_seconds() == 600       # local mode
    _signer(monkeypatch, caps=X402_SIGNER)
    assert _max_authorization_seconds() == 300


def test_a_failed_defi_ceiling_resolve_fails_closed(monkeypatch):
    import core.wallet.tx_guard as tg
    from core.config_policy.spend_lane import autonomous_ceiling_usd

    def _boom(*a, **k):
        raise RuntimeError("prefs unreadable")
    monkeypatch.setattr(tg, "autonomous_max_usd", _boom)
    assert autonomous_ceiling_usd() == 0.0


def test_the_tx_guard_backstop_fallback_stays_under_the_signer(monkeypatch):
    import core.wallet.config as cfg
    from core.wallet.tx_guard import autonomous_max_usd

    def _boom(*a, **k):
        raise RuntimeError("resolver down")
    _signer(monkeypatch, caps=PROD_SIGNER)
    monkeypatch.setattr(cfg, "effective_max_per_tx_usd", _boom)
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "500")
    monkeypatch.setenv("AGENT_WALLET_MAX_PER_TX_USD", "3000")
    assert autonomous_max_usd() == 120.0


def test_provision_ignores_agent_writable_preferences(tmp_path, monkeypatch):
    from core.signer.provision import current_caps
    _pref(tmp_path, monkeypatch)
    _signer(monkeypatch, caps=PROD_SIGNER)
    assert current_caps(PROD_ENV) == {"per_tx_usd": 120.0, "daily_usd": 7000.0}


# ---- review follow-ups: last good caps, short failure TTL --------------------

def test_a_failed_ping_keeps_the_last_good_caps(monkeypatch):
    state = {"fail": False}
    monkeypatch.setattr(signer_pkg, "signer_mode", lambda: "shadow")

    class _C:
        def __init__(self, **kw):
            pass

        def call(self, op):
            if state["fail"]:
                raise client_mod.SignerUnavailable("down")
            return {"caps": PROD_SIGNER}
    monkeypatch.setattr(client_mod, "SignerClient", _C)
    clock = [1000.0]
    monkeypatch.setattr(env_mod.time, "monotonic", lambda: clock[0])
    assert env_mod.envelope("per_tx_usd") == 120.0
    state["fail"] = True
    clock[0] += env_mod._TTL_SEC + 1
    assert env_mod.envelope("per_tx_usd") == 120.0       # last good, not "no clamp"


def test_a_failure_is_retried_after_the_short_ttl(monkeypatch):
    calls = _signer(monkeypatch, fail=True)
    clock = [1000.0]
    monkeypatch.setattr(env_mod.time, "monotonic", lambda: clock[0])
    assert env_mod.signer_caps() == dict.fromkeys(env_mod.LEGS, 0.0)
    clock[0] += env_mod._FAIL_TTL_SEC - 1
    env_mod.signer_caps()
    assert len(calls) == 1
    clock[0] += 2
    env_mod.signer_caps()
    assert len(calls) == 2


# ---- a signer that never answered is "down", not "a $0 cap to raise" -------

def test_readouts_never_show_the_zero_clamp_as_a_signer_cap(tmp_path, monkeypatch):
    """The gate clamps to $0 while a signer has never answered (fail closed).
    The readouts must say the signer is down, not tell the owner to raise a
    $0 cap in signer.toml (the remedy is to start the signer)."""
    import core.wallet.config as cfg
    from cli.commands.wallet import _signer_bound
    from surfaces.telegram.owner_ops import _signer_bound_lines
    _signer(monkeypatch, fail=True)
    monkeypatch.setattr(cfg, "configured_caps_now", lambda *a, **k: (3000.0, 7000.0))
    assert env_mod.signer_caps() == dict.fromkeys(env_mod.LEGS, 0.0)   # gate: closed
    assert env_mod.reported_caps() is None
    assert env_mod.signer_unanswered() is True
    assert _signer_bound("per-tx", "u1", tmp_path) is None
    lines = _signer_bound_lines("u1")
    assert len(lines) == 1 and "has not answered" in lines[0]
    assert "signer.toml" not in lines[0]


def test_reported_caps_are_the_last_good_answer(monkeypatch):
    _signer(monkeypatch, caps=PROD_SIGNER)
    assert env_mod.reported_caps()["per_tx_usd"] == 120.0
    assert env_mod.signer_unanswered() is False
    _signer(monkeypatch, mode="local")
    assert env_mod.reported_caps() is None and env_mod.signer_unanswered() is False
