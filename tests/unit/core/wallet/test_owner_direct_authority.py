"""Owner intent in a genuine owner turn is authorization (owner-UX review, 2026-09-26).

Live: the owner asked his agent to send 0.9976 ETH and could not, for three
hours. "Stop" had paused everything — including his own seat — and after
`/resume` the $300 autonomous ceiling returned ``lane="owner_queue"``, which the
trade verbs print as NOT SENT and nothing consumes.

The rule these tests pin, in `core.wallet.tx_guard` step 0:

* the pause (steps 1/1b and the ledger's own copy) and the autonomous ceiling
  (step 9) bound what the agent does ON ITS OWN;
* a genuine owner turn is not bound by them;
* an owner grant for THIS call clears the ceiling for an autonomous run;
* the hard bounds — declared max, per-tx, daily — bind everyone;
* a missing context (the remote signer, an internal call) is NEVER the owner.
"""
from types import SimpleNamespace

import pytest

from core.money import ledger as money_ledger
from core.money.authority import owner_direct_turn
from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

OWNER = "owner-1"
TO = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"
HOLDER = "0x2222222222222222222222222222222222222222"
ETH_PRICE = 2000.0
AMOUNT = 10 ** 18  # 1 ETH = $2,000, far above the $25 default ceiling


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda: OWNER)
    # The autonomous lane exists only when armed; these tests exercise it.
    monkeypatch.setenv("DEFI_AUTONOMOUS_TURN_TRADING", "true")


def _ctx(**kw):
    base = dict(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                session_id="s-owner", metadata={})
    base.update(kw)
    return SimpleNamespace(**base)


def _not_forged(ctx, tool):
    return False


def _forged(ctx, tool):
    return True


def _gate(**kw):
    base = dict(max_per_tx_usd=5000.0, daily_cap_usd=10000.0)
    base.update(kw)
    return PolicyGate(**base)


def _authorize(ctx, *, forged_fn=_not_forged, halted=False, entry_paused=False,
               max_spend_usd=2500.0, gate=None, autonomous_ok=False):
    intent = tx_guard.TxIntent(chain="base", token=None, to=TO, amount_raw=AMOUNT,
                               max_spend_usd=max_spend_usd,
                               expected_allowance_grants=(), idempotency_key=None)
    return tx_guard.authorize(
        intent, {"to": TO, "data": "0x", "value": AMOUNT, "chainId": 8453},
        holder=HOLDER, gate=gate or _gate(), execution_context=ctx,
        simulate_fn=lambda **_: Deltas(ok=True, native_delta=-AMOUNT, token_deltas={},
                                       allowance_deltas={}, gas_used=21_000),
        price_fn=lambda chain, addr: ETH_PRICE, fallback_price_fn=None,
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: halted, entry_paused_fn=lambda: entry_paused,
        forged_fn=forged_fn,
        autonomous_ok_fn=(lambda ctx, tool: autonomous_ok))


# -- the predicate -----------------------------------------------------------

def test_none_is_never_the_owner():
    """The remote signer re-runs the guard with no context (A1)."""
    assert owner_direct_turn(None, _not_forged) is False


def test_no_detector_is_never_the_owner():
    assert owner_direct_turn(_ctx(), None) is False


@pytest.mark.parametrize("ctx", [
    _ctx(role="leaf"),
    _ctx(is_sub_agent=True),
    _ctx(user_id="stranger"),
    SimpleNamespace(user_id=OWNER),          # no role → least-privileged leaf
])
def test_leaf_subagent_stranger_are_not_the_owner(ctx):
    assert owner_direct_turn(ctx, _not_forged) is False


def test_a_forged_or_autonomous_turn_is_not_the_owner():
    assert owner_direct_turn(_ctx(), _forged) is False


def test_a_raising_detector_is_not_the_owner():
    def boom(ctx, tool):
        raise RuntimeError("probe down")
    assert owner_direct_turn(_ctx(), boom) is False


def test_the_owner_turn_and_the_owner_seat_are_the_owner():
    assert owner_direct_turn(_ctx(), _not_forged) is True
    assert owner_direct_turn(_ctx(role="owner"), _not_forged) is True


# -- the guard ---------------------------------------------------------------

def test_owner_turn_above_the_ceiling_is_authorized():
    d = _authorize(_ctx())
    assert d.allowed is True, d.reason
    assert d.lane == "owner_direct"
    assert d.amount_usd == pytest.approx(2000.0)


def test_owner_turn_under_the_pause_is_authorized():
    d = _authorize(_ctx(), halted=True)
    assert d.allowed is True, d.reason


def test_owner_turn_under_the_trading_pause_is_authorized():
    d = _authorize(_ctx(), entry_paused=True)
    assert d.allowed is True, d.reason


def test_the_ledger_pause_copy_does_not_refuse_the_owner(monkeypatch):
    """The ledger read the agent's pause record directly; the guard now scopes its
    own answer around ``gate.check`` (A2)."""
    monkeypatch.setattr(money_ledger.AutonomyConfig, "autonomy_halted",
                        staticmethod(lambda: True))
    d = _authorize(_ctx(), halted=True)
    assert d.allowed is True, d.reason


def test_the_pause_still_binds_no_context():
    d = _authorize(None, halted=True)
    assert d.allowed is False
    assert "pause" in d.reason.lower()


def test_the_pause_still_binds_an_autonomous_run():
    d = _authorize(_ctx(), forged_fn=_forged, autonomous_ok=True, halted=True)
    assert d.allowed is False
    assert "pause" in d.reason.lower()


def test_the_ceiling_still_binds_an_autonomous_run():
    d = _authorize(_ctx(), forged_fn=_forged, autonomous_ok=True)
    assert d.allowed is False
    assert d.lane == "owner_queue"


def test_the_ceiling_still_binds_no_context():
    """The signer path: ``owner_queue`` there is its documented pass-through."""
    d = _authorize(None)
    assert d.allowed is False
    assert d.lane == "owner_queue"


def test_the_hard_caps_still_bind_the_owner():
    d = _authorize(_ctx(), gate=_gate(max_per_tx_usd=1000.0))
    assert d.allowed is False
    assert "ceiling" in d.reason.lower()


def test_the_daily_cap_still_binds_the_owner():
    d = _authorize(_ctx(), gate=_gate(daily_cap_usd=1500.0))
    assert d.allowed is False
    assert "daily" in d.reason.lower()


def test_the_declared_bound_still_binds_the_owner():
    d = _authorize(_ctx(), max_spend_usd=1000.0)
    assert d.allowed is False
    assert "max_spend_usd" in d.reason


# -- the owner grant (an approved autonomous spend is SENT) --------------------

def _granted(max_spend_usd=2500.0):
    return _ctx(metadata={tx_guard.OWNER_GRANT_KEY: {
        "approved": True, "max_spend_usd": max_spend_usd}})


def test_an_owner_grant_clears_the_ceiling_for_an_autonomous_run():
    d = _authorize(_granted(), forged_fn=_forged, autonomous_ok=True)
    assert d.allowed is True, d.reason
    assert d.lane == "owner_approved"


def test_a_grant_below_the_declared_bound_does_not_cover_it():
    d = _authorize(_granted(max_spend_usd=100.0), forged_fn=_forged, autonomous_ok=True)
    assert d.allowed is False
    assert d.lane == "owner_queue"


@pytest.mark.parametrize("grant", [
    {"approved": False, "max_spend_usd": 5000.0},
    {"approved": True},
    {"approved": True, "max_spend_usd": True},
    "yes",
])
def test_a_malformed_grant_does_not_count(grant):
    ctx = _ctx(metadata={tx_guard.OWNER_GRANT_KEY: grant})
    d = _authorize(ctx, forged_fn=_forged, autonomous_ok=True)
    assert d.lane == "owner_queue"


def test_a_grant_does_not_lift_the_pause():
    d = _authorize(_granted(), forged_fn=_forged, autonomous_ok=True, halted=True)
    assert d.allowed is False
    assert "pause" in d.reason.lower()


# -- the ledger probe is scoped ----------------------------------------------

def test_the_pause_probe_is_restored_after_the_call():
    assert money_ledger._PAUSE_PROBE.get() is None
    with money_ledger.pause_probe(lambda: False):
        assert money_ledger._PAUSE_PROBE.get() is not None
    assert money_ledger._PAUSE_PROBE.get() is None


def test_the_ledger_reads_the_scoped_probe():
    gate = _gate()
    with money_ledger.pause_probe(lambda: True):
        d = gate.check(venue="defi", amount_usd=1.0, idempotency_key=None)
    assert d.allowed is False and "paused" in d.reason


# -- the controller carries the grant, for one action only ----------------------

def test_the_approval_hook_stamps_the_grant_on_approval():
    import asyncio
    from tools.controller.approval import make_approval_hook

    class _Yes:
        decides_as_owner = True

        async def request(self, action, params, ctx):
            return True

    ctx = _ctx()
    hook = make_approval_hook(_Yes(), ["defi_trade_transfer"], timeout=5)
    assert asyncio.run(hook("defi_trade_transfer", {"max_spend_usd": 2800.0}, ctx)) is None
    assert ctx.metadata[tx_guard.OWNER_GRANT_KEY] == {"approved": True, "max_spend_usd": 2800.0}


def test_a_denied_or_undeclared_call_gets_no_grant():
    import asyncio
    from tools.controller.approval import make_approval_hook

    class _No:
        async def request(self, action, params, ctx):
            return False

    class _Yes:
        decides_as_owner = True

        async def request(self, action, params, ctx):
            return True

    ctx = _ctx()
    asyncio.run(make_approval_hook(_No(), ["x"], timeout=5)("x", {"max_spend_usd": 9.0}, ctx))
    asyncio.run(make_approval_hook(_Yes(), ["x"], timeout=5)("x", {}, ctx))
    assert tx_guard.OWNER_GRANT_KEY not in ctx.metadata


def test_the_controller_drops_a_grant_before_the_next_action():
    src = open("tools/controller/execution.py").read()
    pre = src.index("_run_pre_tool_call_hooks(action_type")
    assert 'pop("owner_grant", None)' in src[:pre], (
        "a grant minted for one action must not reach the next one")
    assert tx_guard.OWNER_GRANT_KEY == "owner_grant"


# -- the daily-cap refusal says when headroom returns (A5) ----------------------

def test_the_daily_cap_refusal_says_when_enough_frees():
    gate = _gate(daily_cap_usd=100.0)
    gate._now = lambda: 100_000.0
    gate._audit = [{"ts": 50_000.0, "amount_usd": 60.0, "venue": "defi"},
                   {"ts": 90_000.0, "amount_usd": 30.0, "venue": "defi"}]
    note = gate._frees_at_note(50.0)
    # dropping the $60 row (at 50_000 + 86_400) leaves $30 + $50 <= $100
    assert note.startswith("; enough frees at ")
    assert gate._frees_at_note(500.0) == ""
