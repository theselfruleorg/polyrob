"""122 W4 — the income allocation rule: set only from the owner seat, signed; P % of each income
is a debt first; one buy when the balance passes the minimum or once a day; a failed buy keeps
the debt; a buy of unknown end keeps its income held."""
import json
import os
from types import SimpleNamespace

import pytest

from core.wallet import income_rule as R
from core.wallet.token_trust import owner_seat_ctx

TOKEN = "0x" + "ab" * 20
NOW = 2_000_000.0


@pytest.fixture
def env(tmp_path, monkeypatch):
    from core.wallet.signer import LocalEoaSigner
    from core.wallet.token_pins import pin
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "alice")
    signer = LocalEoaSigner(os.urandom(32))
    monkeypatch.setattr(R, "_signer", lambda: signer)
    pins = str(tmp_path / "pins.db")
    pin("base", TOKEN, "PNL", db_path=pins)
    return SimpleNamespace(path=str(tmp_path / "wallet" / R.STORE_NAME), pins=pins)


def _set(env, ctx=None, **kw):
    base = dict(kinds=["fees", "nft", "payments"], percent=25, chain="base", token=TOKEN,
                min_usd=10, path=env.path, now=NOW, pins_db=env.pins)
    base.update(kw)
    return R.set_rule(ctx or owner_seat_ctx("alice"), **base)


def _ev(key, usd, kind="fees", at=NOW + 1):
    return R.IncomeEvent(key=key, kind=kind, at=at, usd=usd, detail=f"income {key}")


def test_only_the_owner_seat_sets_the_rule_never_the_agent(env):
    with pytest.raises(R.RuleError):
        _set(env, owner_seat_ctx("mallory"))
    # An autonomous owner-tenant run (a goal, a cron job) passes owner_turn_refusal — it is
    # still not the owner seat, so it cannot write the rule.
    agent = SimpleNamespace(user_id="alice", role="orchestrator", is_sub_agent=False,
                            session_id="goal-1", metadata={"turn_id": "t"})
    with pytest.raises(R.RuleError, match="owner seat"):
        _set(env, agent)
    assert not os.path.exists(env.path)
    rule = _set(env)
    assert rule.percent == 25 and rule.symbol == "PNL" and rule.chain == "base"


def test_the_token_must_be_pinned(env):
    with pytest.raises(R.RuleError, match="not a pinned token"):
        _set(env, token="0x" + "cd" * 20)


def test_bad_shapes_refuse(env):
    with pytest.raises(R.RuleError):
        _set(env, percent=0)
    with pytest.raises(R.RuleError):
        _set(env, percent=101)
    with pytest.raises(R.RuleError):
        _set(env, min_usd=0.5)
    with pytest.raises(R.RuleError, match="realized"):
        _set(env, kinds=["trading"])            # no baseline read: refused, never guessed
    assert R.parse_kinds("all") == list(R.KINDS)
    with pytest.raises(R.RuleError):
        R.parse_kinds("fees,salary")


def test_a_changed_rule_does_not_verify_and_allocates_nothing(env):
    _set(env)
    state = R.load(env.path)
    assert R.rule_of(state) is not None
    state["rule"]["percent"] = 100
    assert R.rule_of(state) is None


def test_debt_first_each_income_counted_once(env):
    rule = _set(env)
    state = R.load(env.path)
    added = R.accrue(state, rule, [_ev("a", 20.0), _ev("a", 20.0), _ev("old", 99.0, at=NOW - 1),
                                   _ev("t", 50.0, kind="trading")], NOW + 5)
    assert [e["key"] for e in added] == ["a"]
    assert R.owed_usd(state) == 5.0
    assert R.accrue(state, rule, [_ev("old", 99.0, at=NOW - 1)], NOW + 6) == []


def test_an_unvalued_income_waits_a_day_then_is_shown_not_counted(env):
    rule = _set(env)
    state = R.load(env.path)
    assert R.accrue(state, rule, [_ev("u", None)], NOW + 10) == []
    assert "u" in state["unvalued"]
    # valued on a later tick: counted
    assert R.accrue(state, rule, [_ev("u", 8.0)], NOW + 20)[0]["owed_usd"] == 2.0
    R.accrue(state, rule, [_ev("v", None)], NOW + 30)
    gave_up = R.accrue(state, rule, [_ev("v", None)], NOW + 30 + R.UNVALUED_GIVE_UP_SEC)
    assert gave_up[0]["state"] == R.UNVALUED and R.owed_usd(state) == 2.0


def test_due_at_the_minimum_or_once_a_day_never_below_the_floor(env):
    rule = _set(env)
    state = R.load(env.path)
    R.accrue(state, rule, [_ev("small", 2.0)], NOW + 1)          # 0.5 owed
    assert R.due(state, rule, NOW + 2 * R.DAY_SEC) is None        # below the floor
    R.accrue(state, rule, [_ev("b", 8.0)], NOW + 2)               # 2.5 owed
    assert R.due(state, rule, NOW + 3) is None
    assert "a day" in R.due(state, rule, NOW + 1 + R.DAY_SEC)
    R.accrue(state, rule, [_ev("c", 40.0)], NOW + 4)              # 12.5 owed
    assert "minimum" in R.due(state, rule, NOW + 5)


def test_a_failed_buy_keeps_the_debt_and_says_so_a_done_buy_names_its_income(env):
    rule = _set(env)
    state = R.load(env.path)
    R.accrue(state, rule, [_ev("a", 40.0), _ev("b", 20.0)], NOW + 1)
    buy = R.start_buy(state, rule, NOW + 2)
    assert buy["usd"] == 15.0 and buy["from"] == ["a", "b"] and R.owed_usd(state) == 0
    assert R.due(state, rule, NOW + 3) is None                    # one buy in flight
    R.finish_buy(state, buy["id"], ok=False, text="refused: paused", now=NOW + 3)
    assert R.owed_usd(state) == 15.0
    assert R.due(state, rule, NOW + 4) is None                    # waits before a retry
    assert R.due(state, rule, NOW + 2 + R.RETRY_SEC)
    lines = "\n".join(R.status_lines(state, rule=rule))
    assert "refused: paused" in lines and "the debt stays" in lines
    again = R.start_buy(state, rule, NOW + 2 + R.RETRY_SEC)
    R.finish_buy(state, again["id"], ok=True, tx="0x" + "1" * 64)
    assert R.owed_usd(state) == 0
    lines = "\n".join(R.status_lines(state, rule=rule))
    assert "income a" in lines and "income b" in lines and "0x111" in lines


def test_trading_counts_only_growth_above_the_mark_set_with_the_rule(env):
    rule = _set(env, kinds=["trading"], realized={"base:treasury:0x1": 100.0})
    state = R.load(env.path)
    totals = {"base:treasury:0x1": {"usd": 90.0}, "base:0xacct:0x2": {"usd": 30.0}}
    evs = R.trading_events(state, totals, NOW + 1)
    assert [(e.usd) for e in evs] == [30.0]                       # a loss counts nothing
    evs = R.trading_events(state, {"base:treasury:0x1": {"usd": 110.0}}, NOW + 2)
    assert evs[0].usd == pytest.approx(10.0)
    R.accrue(state, rule, evs, NOW + 2)
    assert R.owed_usd(state) == 2.5


def test_ending_keeps_the_debt_and_an_unknown_buy_waits_for_the_owner(env):
    rule = _set(env)
    state = R.load(env.path)
    R.accrue(state, rule, [_ev("a", 40.0)], NOW + 1)
    buy = R.start_buy(state, rule, NOW + 2)
    R.mark_unknown(state, buy["id"], "restart")
    assert R.open_buy(state) is None                              # does not block the next buy
    R.save(env.path, state)
    R.end_rule(owner_seat_ctx("alice"), path=env.path, now=NOW + 3)
    state = R.load(env.path)
    assert R.rule_of(state) is None and state["ended"][-1]["ended_at"] == NOW + 3
    with pytest.raises(R.RuleError):
        R.settle_unknown(owner_seat_ctx("mallory"), buy["id"], landed=False, path=env.path)
    R.settle_unknown(owner_seat_ctx("alice"), buy["id"], landed=False, path=env.path)
    assert R.owed_usd(R.load(env.path)) == 10.0


def test_an_unreadable_store_raises_never_reads_as_no_rule(env):
    os.makedirs(os.path.dirname(env.path), exist_ok=True)
    with open(env.path, "w") as fh:
        fh.write("{not json")
    with pytest.raises(Exception):
        R.load(env.path)
    with open(env.path, "w") as fh:
        json.dump({"version": 9}, fh)
    with pytest.raises(ValueError):
        R.load(env.path)


def test_the_buy_trim_never_drops_an_open_or_unknown_buy(env):
    """A debt is never dropped in silence: an unknown buy outlives 100 later buys."""
    state = R.empty_state()
    unknown = {"id": "b_unknown", "state": R.B_UNKNOWN}
    state["buys"] = [unknown] + [{"id": f"b{i}", "state": R.B_DONE}
                                 for i in range(R.KEEP_BUYS + 5)]
    os.makedirs(os.path.dirname(env.path), exist_ok=True)
    R.save(env.path, state)
    kept = R.load(env.path)["buys"]
    assert kept[0] == unknown and len(kept) == R.KEEP_BUYS + 1
    assert kept[-1]["id"] == f"b{R.KEEP_BUYS + 4}"
