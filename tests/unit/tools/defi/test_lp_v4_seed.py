"""117 W2/W3: ``lp_add(protocol='v4')`` on a PoolKey the owner declares.

A key named in full is a PROPOSAL (a card, nothing sent). ``seed=<ref>`` builds
the one-transaction seed — or the plain mint on a pool that exists — from the
CONFIRMED row only. The guard is faked here; its own rules are in
tests/unit/core/wallet/test_liquidity_guard_v4_seed.py, and each plan built here
is checked against the real ``structural``."""
import time

import pytest

from core.surfaces import cards
from core.wallet import dex_registry, liquidity_guard as G, lp_seed as S, univ3_math as M
from core.wallet.tx_guard import Decision
from surfaces.telegram import lp_ops
from tools.defi import lp_reads as R, lp_v4 as V4, lp_v4_seed as SEED, lp_verbs as LV
from tools.defi.trade_tool import DefiTradeTool, LpAddParams
from tests.unit.tools.defi.test_deploy_and_call_verbs import _Gate, _Rail, _Wallet, HOLDER

ROW = dex_registry.row_for("robinhood", "v4")
NPM, PM = ROW.position_manager, ROW.pool_manager
ZERO = "0x" + "0" * 40
TKN = "0x" + "55" * 20
PID = G.v4_pool_id((ZERO, TKN, 10000, 200, ZERO))
PRICE = 6_400_000.0                       # TKN per native
ANCHOR = M.sqrt_price_from_price(PRICE, 18, 18)
BIG = 10 ** 30


@pytest.fixture(autouse=True)
def world(monkeypatch):
    monkeypatch.setenv(LV.FLAG, "true")
    monkeypatch.setenv("LP_ETH_CAP", "10")
    monkeypatch.setenv("LP_ETH_FLOOR", "0")
    monkeypatch.setattr(cards, "_STORE", None)
    monkeypatch.setattr(dex_registry, "verify_pins", lambda *a: None)
    monkeypatch.setattr("core.instance.is_owner_uid", lambda uid: uid == "owner")
    monkeypatch.setattr(SEED, "_uid", lambda ctx: "owner")
    state = {"sqrt": 0, "pushed": [], "liquidity": None}
    monkeypatch.setattr(V4, "pool_state", lambda *a: V4.V4PoolState(
        PID, state["sqrt"], 0, 0, 10000, 0, "state_view"))
    monkeypatch.setattr(R, "decimals", lambda rpc, chain, t: 18)
    monkeypatch.setattr(R, "allowance", lambda *a: BIG)
    monkeypatch.setattr(V4, "permit2_allowance", lambda *a: (BIG, int(time.time()) + 9999))
    monkeypatch.setattr(R, "view", lambda rpc, to, spec, args=None:
                        "TKN" if spec["name"] == "symbol" else BIG)

    async def push(container, user_id, card, session_id=None):
        state["pushed"].append(card)
        return "sent"
    monkeypatch.setattr(cards, "push", push)
    return state


def _tool(captured, allow=True, rail=_Rail):
    gate = _Gate()
    gate.audit_log = []

    def guard(intent, tx, **kw):
        captured.append((intent, tx))
        return Decision(allow, "test", "autonomous", 0.01, 400_000)
    t = DefiTradeTool(wallet=_Wallet(gate), rail_factory=rail, guard_fn=guard,
                      price_fn=lambda *a: 3000.0, fallback_price_fn=lambda *a: None)
    t._lp_rpc = lambda method, params=None, **kw: hex(BIG) if method == "eth_getBalance" else {"logs": []}
    t._notify_tx = lambda *a, **kw: None
    t._container = object()            # the delivery rail is faked (cards.push)
    return t, gate


def _declare(**kw):
    return LpAddParams(**(dict(chain="robinhood", protocol="v4", token_a="native", token_b=TKN,
                               amount_a=2.0, amount_b=12_750_000.0, fee=10000, tick_spacing=200,
                               range="ticks:-160000,380000", initial_price=PRICE,
                               max_spend_usd=1.0) | kw))


def _run(ref, **kw):
    return LpAddParams(**(dict(chain="robinhood", protocol="v4", seed=ref,
                               max_spend_usd=20000.0) | kw))


async def _proposed(t):
    res = await t.lp_add(_declare())
    assert res.extracted_content, res.error
    return res.extracted_content.split("Declaration ")[1].split(" ")[0]


# ---- the proposal --------------------------------------------------------

@pytest.mark.asyncio
async def test_a_named_pool_key_is_a_proposal_and_sends_nothing(world):
    captured = []
    t, gate = _tool(captured)
    res = await t.lp_add(_declare(dry_run=False))           # even "live": a card, not a send
    text = res.extracted_content
    assert text.startswith("NOT sent") and "PROPOSED" in text and PID in text
    assert not captured and not gate.recorded and not _Rail.last.sent
    card, = world["pushed"]
    ref = card.line.split()[-1]
    assert card.line == f"/lp seed confirm {ref}" and ref in text
    assert "does NOT exist yet" in card.body and "the AGENT proposed" in card.body
    assert "value: about $12,000.00" in card.body          # 2 native at 3000, both legs
    row = S.get(ref, user_id="owner")
    assert row["state"] == S.PROPOSED and row["fields"]["sqrt_price_x96"] == str(ANCHOR)
    assert (row["fields"]["tick_lower"], row["fields"]["tick_upper"]) == (-160000, 380000)


@pytest.mark.asyncio
@pytest.mark.parametrize("change,why", [
    (dict(initial_price=None), "needs initial_price"),
    (dict(range="ticks:-160001,380000"), "multiples of 200"),
    (dict(range="1,2"), "'full' or 'ticks:lo,hi'"),
    (dict(hooks="0x" + "77" * 20), "no pinned code hash"),
    (dict(token_b="native"), "two different currencies"),
    (dict(token_id=5), "NEW position"),
    (dict(range="ticks:200000,380000", amount_a=0.0), "no liquidity"),
])
async def test_a_declaration_that_cannot_stand_makes_no_card(world, change, why):
    captured = []
    t, _ = _tool(captured)
    res = await t.lp_add(_declare(**change))
    assert why in res.error and "Nothing was proposed" in res.error
    assert not world["pushed"] and not captured


@pytest.mark.asyncio
async def test_a_tenant_that_is_not_the_owner_cannot_propose(world, monkeypatch):
    monkeypatch.setattr(SEED, "_uid", lambda ctx: "someone")
    t, _ = _tool([])
    res = await t.lp_add(_declare())
    assert "not the owner" in res.error and not world["pushed"]


def test_params_need_a_pair_or_a_declaration():
    with pytest.raises(ValueError, match="seed="):
        LpAddParams(chain="robinhood", protocol="v4", max_spend_usd=1.0)
    with pytest.raises(ValueError, match="protocol='v4'"):
        LpAddParams(chain="robinhood", seed="s0123456789", max_spend_usd=1.0)
    with pytest.raises(ValueError, match="protocol='v4'"):
        _declare(protocol="v3")
    with pytest.raises(ValueError):
        _run("../../etc")


# ---- the plan and the send -----------------------------------------------

@pytest.mark.asyncio
async def test_a_dry_run_shows_the_plan_of_an_unconfirmed_declaration(world):
    captured = []
    t, gate = _tool(captured)
    ref = await _proposed(t)
    res = await t.lp_add(_run(ref))
    text = res.extracted_content
    assert "DRY RUN" in text and "NOT CONFIRMED" in text and "creates it at the confirmed price" in text
    intent, tx = captured[0]
    m = G.decode_v4_mint(tx)
    assert (m["seed_sqrt_price"], m["pool_id"]) == (ANCHOR, PID)
    assert (m["tick_lower"], m["tick_upper"]) == (-160000, 380000)
    assert m["amount0_max"] <= 2 * 10 ** 18 and m["amount1_max"] <= 12_750_000 * 10 ** 18
    assert tx["value"] == m["amount0_max"] and tx["to"] == NPM
    assert {e[1] for e in intent.expected_events} == {V4.TOPIC_MODIFY_LIQUIDITY, G.TOPIC_V4_INITIALIZE}
    # the plan the verb built is the seed shape the guard allows
    G.structural(intent, dict(tx, chainId=4663), holder=HOLDER)
    assert not _Rail.last.sent and S.get(ref, user_id="owner")["state"] == S.PROPOSED


@pytest.mark.asyncio
async def test_a_live_send_needs_the_owners_confirm(world):
    captured = []
    t, _ = _tool(captured)
    ref = await _proposed(t)
    res = await t.lp_add(_run(ref, dry_run=False))
    assert "PROPOSED" in res.error and "NOT SENT" in res.error and not captured


@pytest.mark.asyncio
async def test_the_caller_names_nothing_but_the_declaration(world):
    captured = []
    t, _ = _tool(captured)
    ref = await _proposed(t)
    await t.lp_add(_run(ref))
    await t.lp_add(_run(ref, token_a="native", token_b="0x" + "66" * 20, amount_a=9.0,
                        amount_b=1.0, fee=500, range="full", initial_price=1.0))
    first, second = (G.decode_v4_mint(c[1]) for c in captured)
    for k in ("pool_id", "tick_lower", "tick_upper", "liquidity", "amount0_max", "amount1_max",
              "seed_sqrt_price"):
        assert first[k] == second[k]


@pytest.mark.asyncio
async def test_confirm_then_send_seeds_once_and_reads_the_chain_back(world, monkeypatch):
    captured = []
    t, gate = _tool(captured)
    ref = await _proposed(t)
    assert "Declared, for ONE use" in lp_ops.seed_confirm_reply("owner", ref)
    monkeypatch.setattr(LV, "receipt_position", lambda plan, raw, holder: 77)

    def position(rpc, chain, token_id):
        liq = G.decode_v4_mint(captured[-1][1])["liquidity"]
        return V4.V4Position(token_id, HOLDER.lower(), {}, PID, -160000, 380000, liq)
    monkeypatch.setattr(V4, "position", position)
    world["sqrt"] = 0
    res = await t.lp_add(_run(ref, dry_run=False))
    text = res.extracted_content
    assert text and "RESULT: success; position #77" in text, res.error
    assert f"declaration {ref}: SEEDED" in text
    for line in ("owner:", "pool id:", "ticks:", "liquidity:"):
        assert any(line in l and "verified" in l for l in text.splitlines()), line
    assert "pool price: UNVERIFIED" in text              # the faked pool still reads empty
    assert _Rail.last.sent and gate.recorded[-1]["action"] == "lp_add"
    assert gate.recorded[-1]["native_raw"] == captured[-1][1]["value"]
    row = S.get(ref, user_id="owner")
    assert (row["state"], row["token_id"]) == (S.SEEDED, "77")
    again = await t.lp_add(_run(ref, dry_run=False))     # one use
    assert "SEEDED" in again.error and len(captured) == 1


@pytest.mark.asyncio
async def test_a_revert_gives_the_declaration_back(world):
    class Reverting(_Rail):
        def await_receipt(self, tx_hash, **kw):
            from core.wallet.broadcast.evm import Receipt
            return Receipt(tx_hash=tx_hash, status="failed", block_number=7, gas_used=1)
    t, _ = _tool([], rail=Reverting)
    ref = await _proposed(t)
    lp_ops.seed_confirm_reply("owner", ref)
    res = await t.lp_add(_run(ref, dry_run=False))
    assert "reverted: nothing moved" in res.extracted_content
    assert S.get(ref, user_id="owner")["state"] == S.CONFIRMED


@pytest.mark.asyncio
async def test_a_refusal_before_the_broadcast_keeps_the_declaration(world):
    class Failing(_Rail):
        def sign_and_send(self, tx):
            raise ValueError("nonce too low")
    t, _ = _tool([], rail=Failing)
    ref = await _proposed(t)
    lp_ops.seed_confirm_reply("owner", ref)
    res = await t.lp_add(_run(ref, dry_run=False))
    assert res.error and S.get(ref, user_id="owner")["state"] == S.CONFIRMED
    denied, _ = _tool([], allow=False)
    res = await denied.lp_add(_run(ref, dry_run=False))
    assert "NOT SENT" in res.extracted_content
    assert S.get(ref, user_id="owner")["state"] == S.CONFIRMED


@pytest.mark.asyncio
async def test_an_lp_cap_of_zero_refuses_the_live_seed(world, monkeypatch):
    monkeypatch.setenv("LP_ETH_CAP", "0")
    captured = []
    t, _ = _tool(captured)
    ref = await _proposed(t)
    lp_ops.seed_confirm_reply("owner", ref)
    assert "LP_ETH_CAP is 0" in (await t.lp_add(_run(ref))).extracted_content   # the dry run says so
    res = await t.lp_add(_run(ref, dry_run=False))
    assert "LP_ETH_CAP is 0" in res.error and not _Rail.last.sent
    assert S.get(ref, user_id="owner")["state"] == S.CONFIRMED


# ---- a pool that already exists ------------------------------------------

@pytest.mark.asyncio
async def test_an_existing_pool_within_the_gap_gets_a_plain_mint(world):
    captured = []
    t, _ = _tool(captured)
    ref = await _proposed(t)
    world["sqrt"] = ANCHOR * 10020 // 10000                 # price +0.4%, gap is 1%
    res = await t.lp_add(_run(ref))
    assert "the pool exists" in res.extracted_content, res.error
    intent, tx = captured[0]
    m = G.decode_v4_mint(tx)
    assert m["seed_sqrt_price"] is None
    assert {e[1] for e in intent.expected_events} == {V4.TOPIC_MODIFY_LIQUIDITY}
    G.structural(intent, dict(tx, chainId=4663), holder=HOLDER)
    G._assert_price_bound(m, world["sqrt"], G.MINT_MAX_GAP_BPS, "the pool price")


@pytest.mark.asyncio
async def test_an_existing_pool_away_from_the_confirmed_price_refuses(world):
    captured = []
    t, _ = _tool(captured)
    ref = await _proposed(t)
    world["sqrt"] = ANCHOR * 102 // 100                     # price +4%
    res = await t.lp_add(_run(ref))
    assert "away from the price the owner confirmed" in res.error and not captured


# ---- the owner's verb ----------------------------------------------------

def test_the_seat_parses_the_three_seed_lines():
    action, v = lp_ops.parse(["seed", "native", TKN, "2", "12750000", "price", "6380000", "fee",
                              "10000", "spacing", "200", "range", "ticks:-160000,380000", "gap", "50"])
    assert action == "add" and v["protocol"] == "v4" and v["tick_spacing"] == 200
    assert (v["initial_price"], v["slippage_bps"], v["chain"]) == (6380000.0, 50, "robinhood")
    assert LpAddParams(**v).seed is None
    assert lp_ops.parse(["seed", "confirm", "s0123456789"]) == ("seed_confirm", {"ref": "s0123456789"})
    action, v = lp_ops.parse(["seed", "run", "s0123456789", "max", "9000", "go", "on", "robinhood"])
    assert (action, v["seed"], v["dry_run"], v["max_spend_usd"]) == ("add", "s0123456789", False, 9000.0)
    for bad in (["seed", "native", TKN, "2", "1", "fee", "10000", "spacing", "200"],
                ["seed", "run", "s0123456789"], ["seed", "confirm"],
                ["seed", "run", "s0123456789", "max", "5", "fee", "3000"]):
        with pytest.raises(ValueError):
            lp_ops.parse(bad)


def test_the_seat_reaches_v4_positions_pools_and_withdrawals():
    assert lp_ops.parse(["positions", "v4", "id", "42"]) == (
        "positions", {"protocol": "v4", "token_id": 42, "chain": "robinhood"})
    assert lp_ops.parse(["pool", "v4", "id", "42"])[1]["token_id"] == 42
    _, v = lp_ops.parse(["pool", "v4", "native", TKN, "fee", "10000", "spacing", "200"])
    assert (v["token_a"], v["token_b"], v["tick_spacing"]) == ("native", TKN, 200)
    assert lp_ops.parse(["remove", "42", "v4", "pct", "50"])[1]["protocol"] == "v4"
    with pytest.raises(ValueError):
        lp_ops.parse(["pool", "v4"])
    with pytest.raises(ValueError):                         # v3 has no spacing word
        lp_ops.parse(["pool", "0x" + "11" * 20, "spacing", "200"])


def test_confirm_is_the_owners_only_and_says_what_it_did(world):
    ref, _ = S.propose("owner", {**_fields(), "price_source": "agent"}, origin="agent")
    assert "Not confirmed" in lp_ops.seed_confirm_reply("someone", ref)
    assert S.get(ref, user_id="owner")["state"] == S.PROPOSED
    text = lp_ops.seed_confirm_reply("owner", ref)
    assert "Nothing was sent" in text and f"/lp seed run {ref} max <usd> go" in text
    assert "Not confirmed" in lp_ops.seed_confirm_reply("owner", ref)     # once


def _fields():
    return {"chain": "robinhood", "currency0": ZERO, "currency1": TKN, "fee": 10000,
            "tick_spacing": 200, "hooks": ZERO, "tick_lower": -160000, "tick_upper": 380000,
            "amount0_raw": 2 * 10 ** 18, "amount1_raw": 12_750_000 * 10 ** 18, "dec0": 18,
            "dec1": 18, "sym0": "ETH", "sym1": "TKN", "sqrt_price_x96": ANCHOR,
            "price_source": "owner", "gap_bps": 100}
