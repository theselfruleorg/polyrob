"""122 W3: the launch pin and the launch limits (``core.wallet.launch_policy``) and the guard
step that holds a marked intent to them (``tx_guard_steps.value.step_7a_launch_ceiling``)."""
import time
from types import SimpleNamespace

import pytest

from core.wallet import launch_policy as L
from core.wallet.tx_guard_steps import value

Q = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
NEW = "0x" + "11" * 20


def _settings(**prefs):
    return L.read_settings(lambda key: prefs.get(key))


def test_the_pair_is_network_and_token():
    assert L.parse_pair(f"base:{Q.upper().replace('0X', '0x')}") == ("base", Q)
    assert L.parse_pair(f"robinhood {L.NATIVE}") == ("robinhood", L.NATIVE)
    assert L.parse_pair("") is None and L.parse_pair(None) is None
    with pytest.raises(ValueError):
        L.parse_pair("base:usdc")


def test_defaults_and_an_unreadable_value():
    s = _settings()
    assert (s.pair, s.per_day, s.max_pool_usd, s.error) == (None, L.DEFAULT_PER_DAY, None, "")
    bad = _settings(**{L.PAIR_KEY: "nonsense"})
    assert bad.error
    # an unreadable preference refuses every launch; it never lifts the pin
    assert L.pair_refusal(bad, "base", (Q,)).startswith("refused")
    assert L.count_refusal(bad, 0) and L.seed_refusal(bad, 1.0)


def test_the_pin_refuses_any_other_pair_by_name():
    s = _settings(**{L.PAIR_KEY: f"base:{Q}"})
    assert L.pair_refusal(s, "base", (Q,)) is None
    assert L.pair_refusal(s, "base", (NEW, Q)) is None            # a seed names both
    why = L.pair_refusal(s, "base", (OTHER,))
    assert f"launch.pair = base:{Q}" in why and OTHER in why
    assert L.pair_refusal(s, "robinhood", (Q,))                   # same token, other network


def test_every_deploy_of_the_day_counts_and_a_reverted_one_does_not():
    now = time.time()
    log = [{"action": "deploy_token", "ts": now - 60, "result_ref": "0xa"},
           {"action": "deploy_token", "ts": now - 90_000},           # yesterday
           {"action": "swap", "ts": now - 10},
           {"action": "deploy_token", "ts": now - 30, "result_ref": "0xb"}]
    assert L.launches_today(log, now=now) == 2
    assert L.launches_today(log, now=now, voided={"0xb"}) == 1
    s = _settings(**{L.PER_DAY_KEY: 2})
    assert L.count_refusal(s, 1) is None and "launch.per_day is 2" in L.count_refusal(s, 2)
    assert L.count_refusal(_settings(**{L.PER_DAY_KEY: 0}), 0)


def test_the_seed_value_limit():
    s = _settings(**{L.POOL_KEY: 100})
    assert L.seed_refusal(s, 99.5) is None
    assert "above launch.max_pool_usd" in L.seed_refusal(s, 100.5)
    assert "unknown" in L.seed_refusal(s, None)
    assert L.seed_refusal(_settings(), None) is None               # no limit set


class _Gate:
    def __init__(self, log=(), readable=True):
        self.audit_log = list(log)
        self._readable = readable

    def _sync_shared_ledger(self):
        return self._readable


def test_an_unreadable_ledger_refuses_the_count():
    intent = SimpleNamespace(chain="base", launch=("deploy", Q))
    why = L.guard_refusal(intent, 1.0, _Gate(readable=False), _settings())
    assert "unreadable" in why


@pytest.fixture
def prefs(monkeypatch):
    table = {}
    monkeypatch.setattr("core.prefs.owner_pref_scope", lambda: ("owner", "/prefs-home"))
    monkeypatch.setattr("core.prefs.load_preferences", lambda *a, **k: {})
    monkeypatch.setattr("core.prefs.resolve", lambda key, *a, **k: table.get(key))
    return table


def test_an_unreadable_or_unresolved_preference_store_refuses_every_launch(monkeypatch):
    """The 2026-11-20 review: never the defaults (no pin, no pool limit)."""
    import core.prefs as P
    from core.prefs import PrefsUnreadable

    def read():
        return L.read_owner_settings(P.owner_pref_scope, P.load_preferences, P.resolve)
    monkeypatch.setattr("core.prefs.owner_pref_scope", lambda: (None, ""))
    assert read().error
    monkeypatch.setattr("core.prefs.owner_pref_scope", lambda: ("owner", "/prefs-home"))

    def broken(*a, **k):
        raise PrefsUnreadable("bad file")
    monkeypatch.setattr("core.prefs.load_preferences", broken)
    assert "could not be read" in read().error


def _ctx(launch, amount=50.0, log=()):
    return SimpleNamespace(intent=SimpleNamespace(chain="base", launch=launch),
                           amount_usd=amount, gate=_Gate(log))


def test_the_guard_step_skips_an_unmarked_intent(prefs):
    prefs[L.PER_DAY_KEY] = 0
    assert value.step_7a_launch_ceiling(_ctx(())) is None


def test_the_guard_step_holds_a_deploy_and_a_seed(prefs):
    prefs[L.PAIR_KEY] = f"base:{Q}"
    prefs[L.POOL_KEY] = 40
    d = value.step_7a_launch_ceiling(_ctx(("deploy", OTHER)))
    assert not d.allowed and "launch.pair" in d.reason
    d = value.step_7a_launch_ceiling(_ctx(("deploy", Q), log=[
        {"action": "deploy_token", "ts": time.time()}]))
    assert not d.allowed and "launch.per_day" in d.reason
    assert value.step_7a_launch_ceiling(_ctx(("deploy", Q))) is None
    d = value.step_7a_launch_ceiling(_ctx(("seed", NEW, Q), amount=50.0))
    assert not d.allowed and "launch.max_pool_usd" in d.reason
    assert value.step_7a_launch_ceiling(_ctx(("seed", NEW, Q), amount=39.0)) is None


def test_an_unknown_mark_refuses(prefs):
    d = value.step_7a_launch_ceiling(_ctx(("mint", Q)))
    assert not d.allowed and "unknown launch mark" in d.reason


def test_the_launch_preferences_are_owner_rows():
    from core import prefs as P
    for key in (L.PAIR_KEY, L.PER_DAY_KEY, L.POOL_KEY):
        assert P.PREF_SCHEMA[key].sensitivity == P.SENSITIVITY_GUARDED
    assert P.validate_pref(L.PER_DAY_KEY, 3)[1] == 3
    assert not P.validate_pref(L.POOL_KEY, -1)[0]
