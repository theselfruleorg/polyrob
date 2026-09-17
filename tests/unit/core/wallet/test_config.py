import pytest

from core.wallet.config import load_wallet_config, TESTNET_FACILITATOR_URL
from core.prefs import write_preference

# _isolate_polyrob_home (G-13) lives in the directory-level conftest.py — it
# covers every file here, not just this one (test_per_venue_cap.py /
# test_operational_venue.py also call load_wallet_config(env) zero-arg).


def test_defaults_are_safe():
    cfg = load_wallet_config({})
    assert cfg.enabled is False
    assert cfg.x402_client_enabled is False
    assert cfg.network == "testnet"
    assert cfg.backend == "local_eoa"
    assert cfg.master_seed is None
    assert cfg.x402_facilitator_url == TESTNET_FACILITATOR_URL
    # H3 (2026-08-22): daily_cap_usd used to default to None ("disabled" —
    # NO aggregate spend bound at all). The per-tx ceiling alone cannot stop a
    # within-ceiling loop, so the default is now finite ($100/24h); write
    # WALLET_DAILY_CAP_USD=none to restore the old unbounded behaviour.
    assert cfg.daily_cap_usd == 100.0


def test_repr_excludes_master_seed():
    """L1: WalletConfig's auto-repr must never embed the raw seed — one
    logger.debug(f"...{cfg}") away from a log leak."""
    marker = "SEEDMARKER" + "x" * 40
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "true",
                              "AGENT_WALLET_MASTER_SEED": marker})
    r = repr(cfg)
    assert marker not in r
    assert "master_seed" not in r  # field omitted from repr entirely


def test_daily_cap_parsed_when_set():
    assert load_wallet_config({"WALLET_DAILY_CAP_USD": "5"}).daily_cap_usd == 5.0
    # H3 (2026-08-22): blank used to mean "disabled" (None); it now means
    # "use the finite default" — same as fully unset. Only an explicit
    # disable sentinel (see test_cap_can_be_disabled_only_by_an_explicit_sentinel)
    # or a genuinely absent key produces the default; garbage now RAISES
    # instead of silently disabling the cap (see
    # test_malformed_daily_cap_raises_naming_the_key below).
    assert load_wallet_config({"WALLET_DAILY_CAP_USD": ""}).daily_cap_usd == 100.0


def test_reads_env():
    env = {
        "AGENT_WALLET_ENABLED": "true",
        "AGENT_WALLET_MASTER_SEED": "x" * 40,
        "AGENT_WALLET_NETWORK": "mainnet",
        "AGENT_WALLET_MAX_PER_TX_USD": "250",
        "X402_CLIENT_ENABLED": "true",
        "X402_CLIENT_FACILITATOR_URL": "https://facilitator.example",
    }
    cfg = load_wallet_config(env)
    assert cfg.enabled is True
    assert cfg.network == "mainnet"
    assert cfg.max_per_tx_usd == 250.0
    assert cfg.x402_client_enabled is True
    assert cfg.x402_facilitator_url == "https://facilitator.example"


# --- G-13: load_wallet_config() (-> PolicyGate) is now a real caller of the
# tighten-only pref/env merge helpers. Matrix per cap: env-only / pref-only /
# both (min wins) / pref invalid or the prefs module raising (env wins, no
# crash). user_id/home_dir are passed explicitly here for a hermetic pref
# store; the zero-arg tests above (no user_id/home_dir) pin that the
# fail-open owner/home resolution never disturbs a bare env-only call. ------

def test_daily_cap_env_only_no_pref_file(tmp_path):
    env = {"WALLET_DAILY_CAP_USD": "10"}
    cfg = load_wallet_config(env, user_id="u1", home_dir=tmp_path)
    assert cfg.daily_cap_usd == 10.0


def test_daily_cap_pref_only_sets_cap_when_env_unset(tmp_path):
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 7.0)
    cfg = load_wallet_config({}, user_id="u1", home_dir=tmp_path)
    assert cfg.daily_cap_usd == 7.0


def test_daily_cap_both_min_wins(tmp_path):
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 50.0)
    cfg = load_wallet_config({"WALLET_DAILY_CAP_USD": "10"}, user_id="u1", home_dir=tmp_path)
    assert cfg.daily_cap_usd == 10.0  # env is tighter here -> env wins
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 3.0)
    cfg = load_wallet_config({"WALLET_DAILY_CAP_USD": "10"}, user_id="u1", home_dir=tmp_path)
    assert cfg.daily_cap_usd == 3.0  # pref is tighter here -> pref wins


def test_daily_cap_pref_module_raising_env_wins_no_crash(tmp_path, monkeypatch):
    import core.wallet.config as wallet_config_mod

    def _boom(*a, **k):
        raise RuntimeError("prefs store unavailable")

    monkeypatch.setattr(wallet_config_mod, "effective_daily_cap_usd", _boom)
    cfg = load_wallet_config({"WALLET_DAILY_CAP_USD": "10"}, user_id="u1", home_dir=tmp_path)
    assert cfg.daily_cap_usd == 10.0  # fail-open: plain env value, no crash


def test_per_tx_cap_env_only_no_pref_file(tmp_path):
    env = {"AGENT_WALLET_MAX_PER_TX_USD": "500"}
    cfg = load_wallet_config(env, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 500.0


def test_per_tx_cap_pref_only_tightens_the_safety_default(tmp_path):
    # unlike the daily cap, unset per-tx has a concrete $1000 default, not None
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 200.0)
    cfg = load_wallet_config({}, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 200.0


def test_per_tx_cap_owner_pref_overrides_env_within_the_daily_cap(tmp_path):
    """Owner decision 2026-09-18: an owner-approved pref replaces the env
    default in EITHER direction; the daily cap is the envelope."""
    env = {"AGENT_WALLET_MAX_PER_TX_USD": "120", "WALLET_DAILY_CAP_USD": "500"}
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 220.0)
    cfg = load_wallet_config(env, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 220.0  # the owner's raise TOOK EFFECT
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 100.0)
    cfg = load_wallet_config(env, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 100.0  # a lower pref still tightens


def test_per_tx_cap_can_never_exceed_the_daily_cap(tmp_path):
    """The whole safety argument now: a single transaction is at most what a
    day may lose, so no raise from chat moves the maximum daily loss."""
    env = {"AGENT_WALLET_MAX_PER_TX_USD": "120", "WALLET_DAILY_CAP_USD": "500"}
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 5000.0)
    cfg = load_wallet_config(env, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 500.0
    # and the daily cap itself is still min-merged: a pref cannot widen it
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 9000.0)
    cfg = load_wallet_config(env, user_id="u1", home_dir=tmp_path)
    assert cfg.daily_cap_usd == 500.0
    assert cfg.max_per_tx_usd == 500.0


def test_per_tx_cap_with_the_daily_cap_disabled_is_the_pref(tmp_path):
    """`WALLET_DAILY_CAP_USD=none` is an explicit operator opt-out of the
    envelope; there is nothing to clamp to."""
    env = {"AGENT_WALLET_MAX_PER_TX_USD": "120", "WALLET_DAILY_CAP_USD": "none"}
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 800.0)
    cfg = load_wallet_config(env, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 800.0


def test_per_tx_cap_pref_invalid_falls_back_to_env(tmp_path):
    # write_preference validates at write time (min_value=0.0) — a negative
    # value is refused outright, so no bad pref ever reaches disk to begin with.
    ok, err = write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", -5.0)
    assert ok is False and err
    cfg = load_wallet_config({"AGENT_WALLET_MAX_PER_TX_USD": "500"}, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 500.0


def test_per_tx_cap_pref_module_raising_env_wins_no_crash(tmp_path, monkeypatch):
    import core.wallet.config as wallet_config_mod

    def _boom(*a, **k):
        raise RuntimeError("prefs store unavailable")

    monkeypatch.setattr(wallet_config_mod, "effective_max_per_tx_usd", _boom)
    cfg = load_wallet_config({"AGENT_WALLET_MAX_PER_TX_USD": "500"}, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 500.0  # fail-open: plain env value, no crash


def test_zero_arg_call_unaffected_by_fail_open_owner_home_resolution():
    """No user_id/home_dir passed => internal fail-open owner/home resolution
    kicks in, but with no real preferences.toml for that (resolved) tenant this
    must stay byte-identical to the plain env value (regression guard for the
    process-level zero-arg call sites in core/wallet/factory.py)."""
    env = {"WALLET_DAILY_CAP_USD": "10", "AGENT_WALLET_MAX_PER_TX_USD": "250"}
    cfg = load_wallet_config(env)
    assert cfg.daily_cap_usd == 10.0
    assert cfg.max_per_tx_usd == 250.0


# --- H3 (2026-08-22): finite daily-cap default, loud malformed cap ----------

def test_daily_cap_has_a_finite_default():
    """H3: an unset WALLET_DAILY_CAP_USD used to mean NO aggregate bound, and the
    per-tx ceiling alone cannot stop a within-ceiling loop (x402's idempotency
    key is URL-keyed, so a loop mints a fresh key every iteration)."""
    from core.wallet.config import load_wallet_config
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "false"})
    assert cfg.daily_cap_usd == 100.0


def test_explicit_daily_cap_wins():
    """Minor 3 (fix round 1, 2026-08-22 review): the original assertion here
    used `25` (below the $100 default), which passes unchanged against the
    pre-H3 code too and so proved nothing about the new default. The
    discriminating case is an explicit value ABOVE the default — the one that
    would break if the cap-resolution logic ever silently clamped an explicit
    operator value down to the default (e.g. via an accidental min-merge)."""
    from core.wallet.config import load_wallet_config
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "false",
                              "WALLET_DAILY_CAP_USD": "500"})
    assert cfg.daily_cap_usd == 500.0
    cfg2 = load_wallet_config({"AGENT_WALLET_ENABLED": "false",
                               "WALLET_DAILY_CAP_USD": "25"})
    assert cfg2.daily_cap_usd == 25.0


def test_malformed_daily_cap_raises_naming_the_key():
    """H3-sub: `1O0` (letter O) silently parsed to None = NO cap, while the owner
    believed a cap was active. The per-tx ceiling already raises; parity."""
    import pytest
    from core.wallet.config import load_wallet_config
    with pytest.raises(ValueError) as exc:
        load_wallet_config({"AGENT_WALLET_ENABLED": "false",
                            "WALLET_DAILY_CAP_USD": "1O0"})
    assert "WALLET_DAILY_CAP_USD" in str(exc.value)


def test_infinite_daily_cap_raises():
    import pytest
    from core.wallet.config import load_wallet_config
    with pytest.raises(ValueError):
        load_wallet_config({"AGENT_WALLET_ENABLED": "false",
                            "WALLET_DAILY_CAP_USD": "inf"})


def test_cap_can_be_disabled_only_by_an_explicit_sentinel():
    """An operator who genuinely wants no aggregate bound must say so in words,
    so the choice is visible in the env file rather than implied by absence.

    Minor 3 (fix round 1, 2026-08-22 review): the original version of this
    test (verbatim from the brief) asserted ONLY the `none` case, which also
    passes against the entirely unmodified pre-H3 code (the old `_opt_float`
    parsed "none" to None too via its unparseable-string fallback) — it
    pinned nothing about the actual H3 change and would not fail if the
    default were reverted to None. This version additionally asserts the two
    things that WOULD fail on a revert: unset resolves to the finite default,
    never None, and "0" is a literal $0 cap — never a disable synonym."""
    from core.wallet.config import load_wallet_config
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "false",
                              "WALLET_DAILY_CAP_USD": "none"})
    assert cfg.daily_cap_usd is None
    unset_cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "false"})
    assert unset_cfg.daily_cap_usd == 100.0
    zero_cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "false",
                                   "WALLET_DAILY_CAP_USD": "0"})
    assert zero_cfg.daily_cap_usd == 0.0


def test_per_tx_default_is_the_lowered_catastrophe_stop():
    from core.wallet.config import load_wallet_config
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "false"})
    assert cfg.max_per_tx_usd == 250.0


def test_has_daily_cap_is_true_by_default():
    """tx_guard's autonomous lane refuses without one; the default must satisfy it."""
    from core.wallet.config import load_wallet_config
    from core.wallet.policy import PolicyGate
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "false"})
    gate = PolicyGate(max_per_tx_usd=cfg.max_per_tx_usd,
                      daily_cap_usd=cfg.daily_cap_usd)
    assert gate.has_daily_cap is True


def test_daily_cap_pref_cannot_widen_above_the_new_default(tmp_path):
    """Controller ruling (H3 threading review): the old code passed
    `env_value=None` to the pref min-merge when WALLET_DAILY_CAP_USD was
    unset, SPECIFICALLY so a pref alone could set a cap where the operator set
    none. Now that "unset" resolves to a finite $100 default, that same
    None-passthrough would let a WIDER pref (e.g. $500) win outright via the
    `env_value is None -> return pref` branch — silently raising the
    effective cap above the operator's (default) ceiling. The fix threads the
    RESOLVED default as the env leg, so `min(pref, default)` still applies."""
    from core.wallet.config import effective_daily_cap_usd
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 500.0)
    # WALLET_DAILY_CAP_USD is unset -> resolves to the $100 default -> a $500
    # pref must NOT be able to raise the effective cap above that default.
    assert effective_daily_cap_usd("u1", tmp_path, env={}) == 100.0
    # A pref that's actually tighter than the default still wins, unaffected.
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 30.0)
    assert effective_daily_cap_usd("u1", tmp_path, env={}) == 30.0


# --- 2026-09-18: the owner caps are LIVE in the gate, not frozen at start --------
#
# `preferences explain` said `applies: live` for budget.wallet_per_tx_usd, and the
# pref was on disk — but PolicyGate had copied the number at construction, so
# nothing the owner approved from chat reached the gate until the next restart.

def _gate(env, tmp_path):
    from core.wallet.policy import PolicyGate
    cfg = load_wallet_config(env, user_id="u1", home_dir=tmp_path)
    return PolicyGate(max_per_tx_usd=cfg.max_per_tx_usd, daily_cap_usd=cfg.daily_cap_usd,
                      cap_resolver=cfg.cap_resolver)


def test_policy_gate_reads_an_owner_raise_without_a_restart(tmp_path):
    env = {"AGENT_WALLET_MAX_PER_TX_USD": "120", "WALLET_DAILY_CAP_USD": "500"}
    gate = _gate(env, tmp_path)
    assert gate.per_tx_cap_usd == 120.0
    assert not gate.check(venue="defi", amount_usd=197.0, idempotency_key="a").allowed
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 220.0)
    assert gate.per_tx_cap_usd == 220.0
    assert gate.check(venue="defi", amount_usd=197.0, idempotency_key="b").allowed


def test_policy_gate_reads_an_owner_tightening_without_a_restart(tmp_path):
    env = {"AGENT_WALLET_MAX_PER_TX_USD": "500", "WALLET_DAILY_CAP_USD": "500"}
    gate = _gate(env, tmp_path)
    assert gate.check(venue="defi", amount_usd=300.0, idempotency_key="a").allowed
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 100.0)
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 150.0)
    assert not gate.check(venue="defi", amount_usd=300.0, idempotency_key="b").allowed
    assert gate.daily_cap_usd == 150.0


def test_a_gate_without_a_resolver_keeps_its_constructed_caps(tmp_path):
    from core.wallet.policy import PolicyGate
    gate = PolicyGate(max_per_tx_usd=42.0, daily_cap_usd=99.0)
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 1.0)
    assert gate.per_tx_cap_usd == 42.0 and gate.daily_cap_usd == 99.0


def test_a_raising_resolver_keeps_the_constructed_caps():
    from core.wallet.policy import PolicyGate

    def _boom():
        raise RuntimeError("store gone")
    gate = PolicyGate(max_per_tx_usd=42.0, daily_cap_usd=99.0, cap_resolver=_boom)
    assert gate.per_tx_cap_usd == 42.0 and gate.daily_cap_usd == 99.0
    assert gate.has_daily_cap
