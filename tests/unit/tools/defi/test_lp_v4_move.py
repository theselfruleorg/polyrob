"""118 W7: moving a v4 position to a new range — one declaration, then the
withdrawal and the mint as two guarded transactions. The step is read from the
chain (the position's liquidity), never from a stored flag."""
from types import SimpleNamespace

import pytest

from core.surfaces import cards
from core.wallet import dex_registry, liquidity_guard as G, lp_seed as S, univ3_math as M
from tools.defi.owner_verbs import lp as lp_ops
from tools.defi import lp_reads as R, lp_v4 as V4, lp_v4_move as MOVE, lp_v4_seed as SEED, lp_verbs as LV
from tools.defi.trade_tool import LpAddParams
from tests.unit.tools.defi.test_deploy_and_call_verbs import HOLDER
from tests.unit.tools.defi.test_lp_v4_seed import _tool


def _fields(p, holder=HOLDER):
    return MOVE.build_fields(p, None, holder, ticks=LV.ticks, symbol=SEED._symbol)

ZERO = "0x" + "0" * 40
TKN = "0x" + "55" * 20
KEY = {"currency0": ZERO, "currency1": TKN, "fee": 10000, "tickSpacing": 200, "hooks": ZERO}
PID = G.v4_pool_id((ZERO, TKN, 10000, 200, ZERO))
SQRT = M.sqrt_price_at_tick(160000)
FULL = M.full_range_ticks(200)
LIQ = 10 ** 21
NPM_ADDR = dex_registry.row_for("robinhood", "v4").position_manager
BIG = 10 ** 30


@pytest.fixture(autouse=True)
def world(monkeypatch):
    import time
    monkeypatch.setenv(LV.FLAG, "true")
    monkeypatch.setenv("LP_ETH_CAP", "10")
    monkeypatch.setenv("LP_ETH_FLOOR", "0")
    monkeypatch.setattr(cards, "_STORE", None)
    monkeypatch.setattr(dex_registry, "verify_pins", lambda *a: None)
    monkeypatch.setattr("core.instance.is_owner_uid", lambda uid, **kw: uid == "owner")
    monkeypatch.setattr(SEED, "_uid", lambda ctx: "owner")
    state = {"liquidity": LIQ, "owner": HOLDER.lower(), "pushed": [], "fees": (0, 0)}
    monkeypatch.setattr(V4, "position_fees", lambda rpc, chain, pos: state["fees"])
    monkeypatch.setattr(V4, "pool_state", lambda *a: V4.V4PoolState(
        PID, SQRT, 160000, 0, 10000, LIQ, "state_view"))
    monkeypatch.setattr(V4, "position", lambda rpc, chain, tid: V4.V4Position(
        tid, state["owner"], KEY, PID, FULL[0], FULL[1], state["liquidity"]))
    monkeypatch.setattr(R, "decimals", lambda rpc, chain, t: 18)
    monkeypatch.setattr(R, "allowance", lambda *a: BIG)
    monkeypatch.setattr(V4, "permit2_allowance", lambda *a: (BIG, int(time.time()) + 9999))
    monkeypatch.setattr(R, "view", lambda rpc, to, spec, args=None:
                        "TKN" if spec["name"] == "symbol" else 18 if spec["name"] == "decimals"
                        else BIG)

    # 126: the agent's move call dry-runs the move for its card; the rails are fake.
    real = LV._perform

    async def dry_remove(tool, p, ctx, verb):
        if verb == "lp_remove" and p.dry_run:
            return SimpleNamespace(error=None, extracted_content="RESULT: DRY RUN.")
        return await real(tool, p, ctx, verb)
    monkeypatch.setattr(LV, "_perform", dry_remove)

    async def push(container, user_id, card, session_id=None):
        state["pushed"].append(card)
        return "sent"
    monkeypatch.setattr(cards, "push", push)
    return state


#: The owner's own chat turn (126: a card goes to the owner only).
OWNER = SimpleNamespace(user_id="owner", role="orchestrator", is_sub_agent=False, metadata={})


def _move(**kw):
    return LpAddParams(**(dict(chain="robinhood", protocol="v4", move_from=7, move_pct=50,
                               range="ticks:150000,170000", max_spend_usd=1.0) | kw))


def _run(ref, **kw):
    return LpAddParams(**(dict(chain="robinhood", protocol="v4", seed=ref,
                               max_spend_usd=1e13) | kw))


def test_the_declaration_is_the_positions_own_pool_and_what_the_part_holds():
    f = _fields(_move())
    out = M.amounts_for_liquidity(SQRT, M.sqrt_price_at_tick(FULL[0]),
                                  M.sqrt_price_at_tick(FULL[1]), LIQ // 2)
    assert (f["amount0_raw"], f["amount1_raw"]) == out
    assert (f["pool_id"], f["tick_lower"], f["tick_upper"]) == (PID, 150000, 170000)
    assert (f["from_token_id"], f["from_pct"], f["from_liquidity"]) == (7, 50, LIQ)
    assert f["price_source"] == "pool" and f["sqrt_price_x96"] == SQRT
    clean = S.validate(dict(f))
    lines = S.summary_lines(clean)
    assert lines[0].startswith("MOVE: first withdraw 50% of position #7")
    assert "stays in the wallet (nothing is swapped)" in lines[1]
    # A range given as two prices (currency1 per currency0) lands on aligned ticks.
    by_price = _fields(_move(range="4000000,12000000"))
    assert by_price["tick_lower"] % 200 == 0 and by_price["tick_lower"] < 160000 < by_price["tick_upper"]


def test_a_move_refuses_the_same_range_an_empty_position_and_another_owner(world):
    with pytest.raises(ValueError, match="already in ticks"):
        _fields(_move(range="full"))
    with pytest.raises(ValueError, match="does not own"):
        _fields(_move(), "0x" + "9" * 40)
    world["liquidity"] = 0
    with pytest.raises(ValueError, match="no liquidity to move"):
        _fields(_move())


def test_move_fields_come_all_three_and_only_at_the_pool_price():
    f = _fields(_move())
    with pytest.raises(S.SeedError, match="pool's own price"):
        S.validate(dict(f, price_source="agent"))
    with pytest.raises(S.SeedError, match="must be a whole number"):
        S.validate({k: v for k, v in f.items() if k != "from_pct"})
    with pytest.raises(S.SeedError, match="percent 1..100"):
        S.validate(dict(f, from_pct=0))


def test_the_step_is_read_from_the_liquidity_and_the_moves_own_mark():
    f = {"from_token_id": 7, "from_pct": 50, "from_liquidity": str(LIQ)}
    assert MOVE.stage(f, LIQ) == "withdraw"
    assert MOVE.stage(f, LIQ - LIQ // 2, "withdraw:0xab") == "mint"
    # A smaller position with NO mark was changed by something else, not this move.
    with pytest.raises(ValueError, match="The position changed"):
        MOVE.stage(f, LIQ - LIQ // 2)
    # A full position WITH a mark: the withdrawal is pending or reverted, never resent.
    with pytest.raises(ValueError, match="not\n? ?sent twice|It is not"):
        MOVE.stage(f, LIQ, "withdraw:0xab")
    with pytest.raises(ValueError, match="The position changed"):
        MOVE.stage(f, LIQ - 1, "withdraw:0xab")


@pytest.mark.asyncio
async def test_a_move_is_a_card_and_sends_nothing(world):
    t, gate = _tool([])
    res = await t.lp_add(_move(), OWNER)
    assert res.error is None and "NOT sent yet: card" in res.extracted_content
    card = world["pushed"][-1]
    assert "Move a pool position" in card.title and "MOVE: first withdraw 50%" in card.body
    assert card.kind == cards.KIND_CALL      # 126: ONE card: the dry run and the exact call
    assert not gate.recorded


async def _confirmed(t):
    res = await t.lp_add(_move(), OWNER)
    ref = res.extracted_content.split("Declaration ")[1].split(" ")[0]
    assert "Declared" in lp_ops.seed_confirm_reply("owner", ref)
    return ref


TX = "0x" + "ab" * 32


def _fake_perform(world, monkeypatch, *, lands=True, remove_text=None, mint=None):
    calls = []

    async def perform(tool, p, ctx, verb):
        calls.append((verb, p.dry_run))
        if verb == "lp_remove":
            assert (p.token_id, p.liquidity_pct, p.protocol) == (7, 50, "v4")
            if p.dry_run:
                return SimpleNamespace(error=None, extracted_content="RESULT: DRY RUN.")
            if remove_text is not None:
                return SimpleNamespace(error=None, extracted_content=remove_text)
            if lands:
                world["liquidity"] = LIQ - LIQ // 2
            return SimpleNamespace(error=None, extracted_content=f"RESULT: success\ntx: {TX}")
        if p.dry_run:
            return SimpleNamespace(error=None, extracted_content="RESULT: DRY RUN.")
        return mint or SimpleNamespace(error=None, extracted_content="RESULT: success; position #9\ntx: 0x" + "cd" * 32)
    monkeypatch.setattr(LV, "_perform", perform)
    return calls


@pytest.mark.asyncio
async def test_a_confirmed_move_withdraws_then_mints(world, monkeypatch):
    t, _ = _tool([])
    ref = await _confirmed(t)
    calls = _fake_perform(world, monkeypatch)
    res = await t.lp_add(_run(ref, dry_run=False))
    assert res.error is None, res.error
    assert calls == [("lp_remove", False), ("lp_add", False)]
    assert "step 1 (withdraw):\nRESULT: success" in res.extracted_content
    assert "step 2 (mint):\nRESULT: success; position #9" in res.extracted_content
    assert S.get(ref, user_id="owner")["tx_hash"] == "withdraw:" + TX


@pytest.mark.asyncio
async def test_a_mint_that_would_refuse_stops_the_move_before_the_withdrawal(world, monkeypatch):
    """The mint is judged against what the withdrawal RETURNS — a short wallet
    balance does not hide a missing grant, a cap or a low max_spend_usd."""
    t, _ = _tool([])
    ref = await _confirmed(t)
    calls = _fake_perform(world, monkeypatch)
    monkeypatch.setattr(V4, "permit2_allowance", lambda *a: (0, 0))
    res = await t.lp_add(_run(ref, dry_run=True))
    # 126: a dry run names the Permit2 grants the live run makes first; it does not refuse.
    assert res.error is None and "the run first makes the Permit2 grant(s)" in res.extracted_content
    calls.clear()
    monkeypatch.setattr(V4, "permit2_allowance", lambda *a: (BIG, 2 ** 40))
    monkeypatch.setenv("LP_ETH_CAP", "0")
    res = await t.lp_add(_run(ref, dry_run=False))
    assert calls == [] and "LP_ETH_CAP is 0" in res.error
    monkeypatch.setenv("LP_ETH_CAP", "10")
    monkeypatch.setenv("LP_ETH_DAILY_CAP", "0.000001")
    res = await t.lp_add(_run(ref, dry_run=False))
    assert calls == [] and "LP_ETH_DAILY_CAP" in res.error
    monkeypatch.delenv("LP_ETH_DAILY_CAP")
    res = await t.lp_add(_run(ref, dry_run=False, max_spend_usd=0.01))
    assert calls == [] and "max_spend_usd" in res.error
    assert world["liquidity"] == LIQ and not S.get(ref, user_id="owner")["tx_hash"]


@pytest.mark.asyncio
async def test_a_withdrawal_is_never_sent_twice(world, monkeypatch):
    t, _ = _tool([])
    ref = await _confirmed(t)
    calls = _fake_perform(world, monkeypatch, lands=False)       # sent, not yet in a block
    res = await t.lp_add(_run(ref, dry_run=False))
    assert calls == [("lp_remove", False)] and "NOT attempted" in res.error
    res = await t.lp_add(_run(ref, dry_run=False))               # the rerun while it is pending
    assert calls == [("lp_remove", False)] and "not sent twice" in res.error
    world["liquidity"] = LIQ - LIQ // 2                          # it landed
    res = await t.lp_add(_run(ref, dry_run=False))
    assert calls == [("lp_remove", False), ("lp_add", False)] and res.error is None
    assert f"done (withdraw:{TX})" in res.extracted_content


@pytest.mark.asyncio
async def test_a_parallel_run_does_not_take_the_withdrawal(world, monkeypatch):
    t, _ = _tool([])
    ref = await _confirmed(t)
    calls = _fake_perform(world, monkeypatch)
    real = S.claim_withdrawal
    monkeypatch.setattr(S, "claim_withdrawal", lambda r: real(r) and False)   # the other run won
    res = await t.lp_add(_run(ref, dry_run=False))
    assert calls == [] and "already taken by another run" in res.error


@pytest.mark.asyncio
async def test_a_withdrawal_that_was_not_sent_gives_the_move_back(world, monkeypatch):
    t, _ = _tool([])
    ref = await _confirmed(t)
    _fake_perform(world, monkeypatch, remove_text="guard: refused\nRESULT: NOT SENT.")
    res = await t.lp_add(_run(ref, dry_run=False))
    assert "Nothing was withdrawn" in res.error
    assert not S.get(ref, user_id="owner")["tx_hash"]
    calls = _fake_perform(world, monkeypatch)
    assert (await t.lp_add(_run(ref, dry_run=False))).error is None and len(calls) == 2


@pytest.mark.asyncio
async def test_an_unknown_withdrawal_outcome_closes_the_move(world, monkeypatch):
    t, _ = _tool([])
    ref = await _confirmed(t)
    calls = _fake_perform(world, monkeypatch, remove_text="broadcast failed: timeout")
    res = await t.lp_add(_run(ref, dry_run=False))
    assert "UNKNOWN" in res.error
    res = await t.lp_add(_run(ref, dry_run=False))
    assert calls == [("lp_remove", False)] and "not sent twice" in res.error


@pytest.mark.asyncio
@pytest.mark.parametrize("text, said", [
    ("guard: over the limit\nRESULT: NOT SENT.", "The mint was NOT sent"),
    ("RESULT: failed\ntx: 0x" + "cd" * 32, "The mint was SENT and is not confirmed"),
])
async def test_a_mint_that_did_not_land_is_an_error_and_says_where_the_funds_are(
        world, monkeypatch, text, said):
    """A guard refusal and a reverted receipt come back as CONTENT from the
    verb; the move must not pass them on as a success."""
    t, _ = _tool([])
    ref = await _confirmed(t)
    _fake_perform(world, monkeypatch, mint=SimpleNamespace(error=None, extracted_content=text))
    res = await t.lp_add(_run(ref, dry_run=False))
    assert res.error and said in res.error and "in the wallet" in res.error


@pytest.mark.asyncio
async def test_a_dry_run_shows_both_steps_and_sends_nothing(world, monkeypatch):
    t, _ = _tool([])
    res = await t.lp_add(_move(), OWNER)
    ref = res.extracted_content.split("Declaration ")[1].split(" ")[0]     # still PROPOSED
    calls = _fake_perform(world, monkeypatch)
    res = await t.lp_add(_run(ref))
    assert calls == [("lp_remove", True)] and res.error is None
    assert "against what the withdrawal returns" in res.extracted_content
    assert not S.get(ref, user_id="owner")["tx_hash"]


@pytest.mark.asyncio
async def test_a_dry_run_the_guard_refuses_is_an_error(world, monkeypatch):
    t, _ = _tool([])
    ref = await _confirmed(t)

    async def refused(tool, p, ctx, verb):
        return SimpleNamespace(error="refused: the withdrawal has no floor", extracted_content=None)
    monkeypatch.setattr(LV, "_perform", refused)
    res = await t.lp_add(_run(ref))
    assert res.error and "would not pass" in res.error and "NOT SENT" in res.error
    assert not S.get(ref, user_id="owner")["tx_hash"]
    # The same at the mint step: the withdrawal landed, the mint's dry run refuses.
    assert S.claim_withdrawal(ref) and S.set_withdrawal(ref, TX)
    world["liquidity"] = LIQ - LIQ // 2
    res = await t.lp_add(_run(ref))
    assert res.error and "step 2 (mint)" in res.error


@pytest.mark.asyncio
async def test_a_trading_pause_stops_a_move_before_its_withdrawal(world, monkeypatch):
    """The mint opens a position, so the scoped pause refuses the MOVE: the
    withdrawal (an exit the pause lets through) is never sent on its own."""
    t, _ = _tool([])
    ref = await _confirmed(t)
    calls = _fake_perform(world, monkeypatch)
    import core.autonomy_control as ac
    monkeypatch.setattr(ac, "pause_refusal_text", lambda kind, what="", **kw: (
        "PAUSED: trading entries" if kind == "trade_entry" else None))
    res = await t.lp_add(_run(ref, dry_run=False))
    assert res.error and "PAUSED" in res.error and "nothing was withdrawn" in res.error
    assert calls == [] and not S.get(ref, user_id="owner")["tx_hash"]


def test_the_seat_parses_a_move():
    action, values = lp_ops.parse(["move", "7", "range", "ticks:150000,170000", "pct", "50", "gap", "300"])
    assert action == "add"
    assert values == {"chain": "robinhood", "protocol": "v4", "range": "ticks:150000,170000",
                      "move_from": 7, "move_pct": 50, "slippage_bps": 300, "max_spend_usd": 1.0}
    LpAddParams(**values)
    with pytest.raises(ValueError, match="give /lp move"):
        lp_ops.parse(["move", "7"])


# -- 118 W5: a liquidity write's dry run is a quote, and a withdrawal is proposable --------

@pytest.mark.asyncio
async def test_a_dry_run_from_the_seat_prints_the_line_its_card_confirms(monkeypatch):
    async def dispatch(user_id, action, values, **kw):
        return SimpleNamespace(error=None, extracted_content="lp_remove …\nRESULT: DRY RUN. Re-run …")
    monkeypatch.setattr(lp_ops, "dispatch", dispatch)
    args = ["remove", "v4", "7", "pct", "50"]
    reply = await lp_ops.lp_reply("owner", args)
    assert reply.endswith("Send it: /lp remove v4 7 pct 50 go")
    assert cards.confirm_line_in("/lp", args, reply) == args
    # A read prints no confirm line, so it is never carded.
    reply = await lp_ops.lp_reply("owner", ["positions", "v4"])
    assert cards.confirm_line_in("/lp", ["positions", "v4"], reply) is None

    async def refused(user_id, action, values, **kw):
        return SimpleNamespace(error=None, extracted_content="guard: no; lane: refuse\nRESULT: NOT SENT.")
    monkeypatch.setattr(lp_ops, "dispatch", refused)
    assert "Send it" not in await lp_ops.lp_reply("owner", args)


def test_the_agent_may_propose_a_withdrawal_or_a_collect_and_nothing_else(monkeypatch, tmp_path):
    st = cards.CardStore(str(tmp_path / "cards.db"))
    card = cards.proposal_card("owner", "/lp", ["remove", "v4", "7", "pct", "50"], why="x", st=st)
    assert card.line == "/lp remove v4 7 pct 50"
    cards.proposal_card("owner", "/lp", ["collect", "v4", "7"], st=st)
    for words in (["add", "a", "b", "1", "1", "max", "5"], ["seed", "run", "sabc", "max", "5"],
                  ["move", "7", "range", "full"]):
        with pytest.raises(ValueError, match="only: collect \\| remove"):
            cards.proposal_card("owner", "/lp", words, st=st)
    with pytest.raises(ValueError, match="never carries `go`"):
        cards.proposal_card("owner", "/lp", ["remove", "7", "go"], st=st)


# -- 118 W6: more liquidity into a held v4 position ---------------------------------------

def _priced(t):
    """A price for each leg that agrees with the pool (the verb checks it)."""
    ratio = (SQRT / M.Q96) ** 2                      # TKN per native
    t._price = lambda chain, token: 3000.0 / ratio if str(token).lower() == TKN else 3000.0
    return t


def _increase(**kw):
    return LpAddParams(**(dict(chain="robinhood", protocol="v4", token_id=7, token_a="native",
                               token_b=TKN, amount_a=0.01, amount_b=200_000.0,
                               max_spend_usd=500.0) | kw))


@pytest.mark.asyncio
async def test_an_increase_builds_the_shape_the_guard_accepts(world, monkeypatch):
    from core.wallet import abi
    captured = []
    t, gate = _tool(captured)
    _priced(t)
    res = await t.lp_add(_increase())
    assert res.error is None and "RESULT: DRY RUN" in res.extracted_content, res.error
    assert "increase: liquidity" in res.extracted_content
    intent, tx = captured[-1]
    assert intent.lp_position == (NPM_ADDR, 7) and intent.lp_position_effect == "hold"
    G.structural(intent, dict(tx, chainId=4663), holder=HOLDER)       # the real rules
    c = G.decode_v4_increase(tx)
    # The price bound of the guard's chain half holds on the position's own range.
    G._assert_price_bound({"tick_lower": FULL[0], "tick_upper": FULL[1], "liquidity": c["liquidity"],
                           "amount0_max": c["amount0_max"], "amount1_max": c["amount1_max"]},
                          SQRT, G.MINT_MAX_GAP_BPS, "the pool price")
    assert tx["value"] == c["amount0_max"] <= 10 ** 16
    # token order does not matter; a foreign token refuses.
    swapped = await t.lp_add(_increase(token_a=TKN, token_b="native", amount_a=200_000.0, amount_b=0.01))
    assert G.decode_v4_increase(captured[-1][1])["liquidity"] == c["liquidity"], swapped.error
    other = await t.lp_add(_increase(token_b="0x" + "66" * 20))
    assert "token_a and token_b must name those two" in other.error
    world["owner"] = "0x" + "9" * 40
    assert "does not own" in (await t.lp_add(_increase())).error


@pytest.mark.asyncio
async def test_an_increase_refuses_when_the_fees_owed_would_revert_it(world):
    """An increase credits the fees owed; SETTLE_PAIR reverts on a currency the
    wallet is then owed. The verb names the collect instead of a revert."""
    t, _ = _tool([])
    _priced(t)
    world["fees"] = (10 ** 15, 5)                 # small against a 0.01 native leg: fine
    assert (await t.lp_add(_increase())).error is None
    world["fees"] = (10 ** 17, 0)                 # more than the native leg pays
    res = await t.lp_add(_increase())
    assert "Collect first: lp_collect(chain='robinhood', protocol='v4', token_id=7)" in res.error

    def unread(*a):
        raise R.LpReadError("no StateView")
    import tools.defi.lp_v4 as mod
    world["fees"] = None
    mod.position_fees = unread                    # the autouse fixture restores it
    assert "were NOT READ" in (await t.lp_add(_increase())).error


@pytest.mark.asyncio
async def test_a_live_increase_meets_the_program_cap(world, monkeypatch):
    monkeypatch.setenv("LP_ETH_CAP", "0")
    t, _ = _tool([])
    res = await _priced(t).lp_add(_increase(dry_run=False))
    assert "LP_ETH_CAP is 0" in res.error


@pytest.mark.asyncio
async def test_a_confirmed_move_makes_its_own_permit2_grants(world, monkeypatch):
    """2026-11-20: the run makes the Permit2 legs itself (ERC-20 -> Permit2, then the
    exact Permit2 grant) and moves; nothing is asked of the owner."""
    import time
    from tools.defi import approve_verb
    t, _ = _tool([])
    ref = await _confirmed(t)
    calls = _fake_perform(world, monkeypatch)
    have = {"erc20": 0, "permit2": 0}
    monkeypatch.setattr(R, "allowance", lambda *a: have["erc20"])
    monkeypatch.setattr(V4, "permit2_allowance",
                        lambda *a: (have["permit2"], int(time.time()) + 900))
    grants = []

    async def approve(tool, p, ctx=None):
        grants.append((p.via, p.spender.lower(), p.dry_run))
        have[p.via] = BIG
        return SimpleNamespace(error=None, extracted_content="RESULT: success")
    monkeypatch.setattr(approve_verb, "perform_approve_token", approve)
    res = await t.lp_add(_run(ref, dry_run=False))
    assert res.error is None, res.error
    permit2 = dex_registry.row_for("robinhood", "v4").permit2.lower()
    assert grants == [("erc20", permit2, False), ("permit2", NPM_ADDR.lower(), False)]
    assert calls == [("lp_remove", False), ("lp_add", False)]


@pytest.mark.asyncio
async def test_the_grants_are_sized_to_the_mints_maxima_not_the_declaration(world, monkeypatch):
    """Audit 2026-11-21 L-10: the ERC-20 allowance to Permit2 has no expiry, so the run
    grants the mint's computed maxima at the price now (lp_v4_seed.mint_bounds), not
    the declared amount x 1.000001 — what the mint does not pull stays a standing claim."""
    import time
    from decimal import Decimal
    from tools.defi import approve_verb, lp_v4_seed
    t, _ = _tool([])
    ref = await _confirmed(t)
    _fake_perform(world, monkeypatch)
    have = {"erc20": 0, "permit2": 0}
    monkeypatch.setattr(R, "allowance", lambda *a: have["erc20"])
    monkeypatch.setattr(V4, "permit2_allowance",
                        lambda *a: (have["permit2"], int(time.time()) + 900))
    asked = []

    async def approve(tool, p, ctx=None):
        asked.append((p.via, p.token.lower(), p.amount))
        have[p.via] = BIG
        return SimpleNamespace(error=None, extracted_content="RESULT: success")
    monkeypatch.setattr(approve_verb, "perform_approve_token", approve)
    rows = []
    real_load = lp_v4_seed.load
    monkeypatch.setattr(lp_v4_seed, "load", lambda p, ctx: rows.append(real_load(p, ctx)) or rows[-1])
    res = await t.lp_add(_run(ref, dry_run=False))
    assert res.error is None, res.error
    from core.wallet import lp_seed as S
    f = rows[0]["fields"]
    _liq, maxima = lp_v4_seed.mint_bounds(f, SQRT)
    key = S.key_of(f)
    by_cur = {key["currency0"].lower(): (maxima[0], int(f["amount0_raw"]), int(f["dec0"])),
              key["currency1"].lower(): (maxima[1], int(f["amount1_raw"]), int(f["dec1"]))}
    erc20 = [(c, a) for via, c, a in asked if via == "erc20"]
    assert erc20
    for cur, amount in erc20:
        mx, declared, dec = by_cur[cur]
        raw = int(round(amount * 10 ** dec))
        assert raw >= mx, "the grant must cover the mint"
        assert raw <= mx + max(2, mx // 10 ** 12) + mx // 10 ** 14, "a hair above the maxima only"
        if mx < declared:
            assert raw < int(Decimal(declared) * Decimal("1.000001"))


def test_the_pinned_permit2_is_a_route_spender_and_nothing_else_is_added():
    """2026-11-20: the v4 Permit2 is pinned like the v3 PositionManager, so the agent
    may grant the ERC-20 leg of a v4 mint without an owner turn."""
    from core.wallet import tx_guard
    pinned = tx_guard.pinned_route_spenders("robinhood")
    assert dex_registry.row_for("robinhood", "v4").permit2.lower() in pinned
    assert NPM_ADDR.lower() not in pinned          # the v4 manager takes Permit2 grants only
    assert "0x" + "9" * 40 not in pinned


@pytest.mark.asyncio
async def test_a_grant_the_guard_refuses_stops_the_move_before_the_withdrawal(world, monkeypatch):
    from tools.defi import approve_verb
    t, _ = _tool([])
    ref = await _confirmed(t)
    calls = _fake_perform(world, monkeypatch)
    monkeypatch.setattr(R, "allowance", lambda *a: 0)

    async def approve(tool, p, ctx=None):
        return SimpleNamespace(error="refused: over the daily cap", extracted_content=None)
    monkeypatch.setattr(approve_verb, "perform_approve_token", approve)
    res = await t.lp_add(_run(ref, dry_run=False))
    assert calls == [] and "erc20 grant" in res.error and "over the daily cap" in res.error
    assert "RESULT: NOT SENT" in res.error and world["liquidity"] == LIQ


# --- audit 2026-11-21 L-11: a move's withdrawal minimums are bound to the anchor ---------

def _remove_plan(monkeypatch, floor=None, token_id=7):
    from tools.defi import lp_v4_verbs as LVV
    from tools.defi.trade_params import LpRemoveParams
    monkeypatch.setattr(dex_registry, "hook_pinned", lambda *a: True)
    monkeypatch.setattr(dex_registry, "verify_hook", lambda *a: None)
    monkeypatch.setattr(LV, "pool_price_check", lambda *a, **k: "pool price check: stub\n")
    p = LpRemoveParams(chain="robinhood", protocol="v4", token_id=token_id, liquidity_pct=50,
                       slippage_bps=100, max_spend_usd=1.0, dry_run=True)
    npm = dex_registry.resolve_position_manager("robinhood", "v4")
    if floor is None:
        return LVV.prepare_remove_v4(p, None, HOLDER, npm)
    with LVV.move_floor(7, floor):
        return LVV.prepare_remove_v4(p, None, HOLDER, npm)


def _spot_amounts():
    return M.amounts_for_liquidity(SQRT, M.sqrt_price_at_tick(FULL[0]),
                                   M.sqrt_price_at_tick(FULL[1]), LIQ // 2)


def test_a_move_withdrawal_whose_spot_return_is_below_the_anchor_floor_refuses(monkeypatch):
    """The pool was walked away from the anchor (the indexer's price of a one-pool token
    follows it): spot minimums alone would pass; the anchor floor refuses before a build."""
    a0, a1 = _spot_amounts()
    with pytest.raises(ValueError, match="the pool moved"):
        _remove_plan(monkeypatch, floor=(a0 * 2, a1 // 2))


def test_a_move_withdrawal_takes_the_anchor_floor_as_its_minimums(monkeypatch):
    a0, a1 = _spot_amounts()
    floor = (a0 - a0 // 1000, a1 - a1 // 1000)   # the anchor, less a 0.1% gap: above spot - 1%
    plan = _remove_plan(monkeypatch, floor=floor)
    mins = [m for _cur, m in plan.intent.lp_inflows]
    assert mins == list(floor)


def test_a_plain_withdrawal_or_another_position_keeps_the_spot_minimums(monkeypatch):
    a0, a1 = _spot_amounts()
    spot = [a0 * 9900 // 10000, a1 * 9900 // 10000]
    assert [m for _c, m in _remove_plan(monkeypatch).intent.lp_inflows] == spot
    other = _remove_plan(monkeypatch, floor=(a0, a1), token_id=8)
    assert [m for _c, m in other.intent.lp_inflows] == spot
