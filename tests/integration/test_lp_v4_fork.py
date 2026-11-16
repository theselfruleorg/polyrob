"""v4 lp_add on the Pons token PoolKey, on a FORK of Robinhood Chain 4663 (core
handoff W6; 048 phase 3; 090 R1/R2).

Opt-in: ``LP_V4_FORK_E2E=1`` and an anvil fork at ``LP_V4_FORK_RPC`` (default
``http://127.0.0.1:8551``)::

    anvil --fork-url https://rpc.mainnet.chain.robinhood.com --port 8551 --chain-id 4663

Nothing is signed. The fork-only setup (the owner's ERC-20 allowance to Permit2,
then the Permit2 grant) is sent with ``anvil_impersonateAccount``; the fork is
snapshotted first and reverted at the end. The tests run the REAL
``tx_guard.authorize`` + ``simulation.simulate`` on:

1. the Permit2 grant ``approve_token(via='permit2')`` builds — allowed; the same
   grant to another spender — refused;
2. the lp_add ``prepare_add_v4`` builds from the live pool — allowed, with the
   predicted position id; a tampered declaration — refused;
3. the same lp_add sent on the fork — it mints the predicted id, so the
   authorization is not vacuous (090 §9: the Pons hook accepts the add).
"""
import json
import os
import time
import urllib.request
from types import SimpleNamespace

import pytest

from core.wallet import dex_registry, tx_guard
from core.wallet.policy import PolicyGate
from tools.defi import lp_reads, lp_v4 as V

#: The holder impersonated on the fork: an address that holds the token on chain
#: 4663. Read from the env, never pinned here (no real wallet in shipped tests).
TREASURY = os.environ.get("LP_V4_FORK_HOLDER", "")
#: The Pons token and its native PoolKey's pool id: read from the env too (no
#: real deployment's token in shipped tests).
TOKEN = os.environ.get("LP_V4_FORK_TOKEN", "")
POOL_ID = os.environ.get("LP_V4_FORK_POOL_ID", "")

pytestmark = pytest.mark.skipif(
    os.environ.get("LP_V4_FORK_E2E") != "1" or not (TREASURY and TOKEN and POOL_ID),
    reason="fork e2e: set LP_V4_FORK_E2E=1, LP_V4_FORK_HOLDER=<a token holder>, "
           "LP_V4_FORK_TOKEN=<the Pons token>, LP_V4_FORK_POOL_ID=<its pool id> "
           "and run anvil (see module doc)")

RPC = os.environ.get("LP_V4_FORK_RPC", "http://127.0.0.1:8551")
ROW = dex_registry.row_for("robinhood", "v4")
ETH_USD = 2690.0


def _rpc(method, params, timeout=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def _send(frm, to, data, value=0):
    _rpc("anvil_impersonateAccount", [frm])
    h = _rpc("eth_sendTransaction", [{"from": frm, "to": to, "data": data, "value": hex(value),
                                      "gas": hex(2_000_000)}])
    for _ in range(50):
        rc = _rpc("eth_getTransactionReceipt", [h])
        if rc:
            assert int(rc["status"], 16) == 1, rc
            return rc
        time.sleep(0.1)
    raise AssertionError("no receipt")


def _price(state):
    s = state.sqrt_price_x96 / 2 ** 96
    token_per_eth = s * s
    return lambda chain, addr: ETH_USD if addr.lower() != TOKEN.lower() else ETH_USD / token_per_eth


@pytest.fixture
def fork(monkeypatch, tmp_path):
    try:
        if int(_rpc("eth_chainId", []), 16) != 4663:
            pytest.skip(f"{RPC} is not a 4663 fork")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no fork at {RPC}: {exc}")
    monkeypatch.setenv("DEFI_EVM_RPC_ROBINHOOD", RPC)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    # The deposits here are ~$50; below this ceiling the guard's lane is
    # autonomous. The default $25 sends them to the owner queue (also allowed=False).
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "300")
    snap = _rpc("evm_snapshot", [])
    _rpc("anvil_setBalance", [TREASURY, hex(10 * 10 ** 18)])
    state = V.pool_state(_rpc, "robinhood", POOL_ID)
    yield {"state": state, "price": _price(state)}
    _rpc("evm_revert", [snap])


def _authorize(intent, tx, price, **kw):
    return tx_guard.authorize(
        intent, tx, holder=TREASURY, gate=PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=5000.0),
        execution_context=None, price_fn=price, rpc_is_pinned_fn=lambda c: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False, liquidity_rpc=_rpc, **kw)


def _grant(amount, spender=ROW.position_manager):
    exp = int(time.time()) + 900
    tx = {"to": ROW.permit2, "from": TREASURY, "value": 0, "chainId": 4663,
          "data": V.encode_permit2_approve(TOKEN, spender, amount, exp)}
    intent = tx_guard.TxIntent(chain="robinhood", token=TOKEN, to=spender, amount_raw=0,
                               max_spend_usd=200.0, is_allowance_op=True,
                               expected_allowance_grants=((TOKEN, spender, amount),),
                               idempotency_key=f"p2-{spender}-{amount}")
    return intent, tx


def test_the_native_key_is_the_live_pool(fork):
    key = V.pons_key_for(_rpc, TOKEN)
    assert key["currency0"] == V.ZERO and lp_reads.pool_id(key) == POOL_ID
    assert fork["state"].sqrt_price_x96 > 0 and fork["state"].liquidity > 0
    dex_registry.verify_pins(_rpc, "robinhood", "v4")
    dex_registry.verify_hook(_rpc, "robinhood", key["hooks"])


def test_permit2_grant_to_the_pinned_posm_is_allowed_and_another_is_refused(fork):
    amount = 10 ** 22                    # small: a grant is priced, and the price moves
    _send(TREASURY, TOKEN, "0x095ea7b3" + ROW.permit2[2:].lower().rjust(64, "0")
          + format(amount, "064x"))          # the owner's one ERC-20 allowance to Permit2
    d = _authorize(*_grant(amount), fork["price"])
    assert d.allowed and d.lane == "autonomous", d.reason
    other = "0x" + "3" * 40
    d = _authorize(*_grant(amount, spender=other), fork["price"])
    assert not d.allowed and "Permit2 Approval" in d.reason, d.reason


def _plan(eth=0.01, tok=1_000_000):
    from tools.defi.lp_v4_verbs import prepare_add_v4
    p = SimpleNamespace(chain="robinhood", token_a="native", token_b=TOKEN, amount_a=eth,
                        amount_b=tok, range="full", initial_price=None, token_id=None,
                        slippage_bps=100, max_spend_usd=200.0, dry_run=True)
    return prepare_add_v4(p, _rpc, TREASURY, ROW.position_manager, price_fn=None)


def test_lp_add_through_the_guard_and_on_the_fork(fork):
    amount = 10 ** 24
    _send(TREASURY, TOKEN, "0x095ea7b3" + ROW.permit2[2:].lower().rjust(64, "0")
          + format(amount, "064x"))
    _send(TREASURY, ROW.permit2, _grant(amount)[1]["data"])
    plan = _plan()
    tx = {"to": ROW.position_manager, "from": TREASURY, "value": plan.value,
          "data": plan.data, "chainId": 4663}
    next_id = int(lp_reads.view(_rpc, ROW.position_manager, V.POSM_NEXT_TOKEN_ID))
    d = _authorize(plan.intent, tx, fork["price"])
    assert d.allowed and d.lane == "autonomous", d.reason
    assert d.position_token_id == next_id
    assert d.amount_usd and d.amount_usd < 200.0

    # a declaration that under-states the calldata's token maximum refuses
    import dataclasses
    low = tuple((t, n - 1 if t else n) for t, n in plan.intent.lp_outflows)
    d2 = _authorize(dataclasses.replace(plan.intent, lp_outflows=low), tx, fork["price"])
    assert not d2.allowed and "may pay" in d2.reason, d2.reason

    # not vacuous: the same bytes mint that id on the fork, through the Pons hook
    rc = _send(TREASURY, ROW.position_manager, plan.data, value=plan.value)
    owner = lp_reads.view(_rpc, ROW.position_manager,
                          {"name": "ownerOf", "inputs": [{"type": "uint256"}],
                           "outputs": [{"type": "address"}]}, [next_id])
    assert str(owner).lower() == TREASURY.lower()
    from core.wallet import liquidity_guard
    liquidity_guard.assert_position_events(plan.intent, rc["logs"], next_id)


# ---- 117: a pool the owner declares — seed, read, withdraw -------------------

SEED_FEE, SEED_SPACING = 3000, 60         # a key next to the live one; no hook


def _declared(fork, monkeypatch, *, eth=0.01):
    """A CONFIRMED declaration of a fresh TOKEN/native key at the live pool's
    price, with a custom range around it."""
    from core.wallet import lp_seed as S
    from tools.defi import lp_v4_seed
    st = fork["state"]
    s = st.sqrt_price_x96 / 2 ** 96
    p = SimpleNamespace(chain="robinhood", protocol="v4", token_a="native", token_b=TOKEN,
                        amount_a=eth, amount_b=eth * s * s * 3, fee=SEED_FEE,
                        tick_spacing=SEED_SPACING, hooks=None, token_id=None,
                        range=f"ticks:{(st.tick - 30000) // 60 * 60},{(st.tick + 30000) // 60 * 60}",
                        initial_price=s * s, slippage_bps=100)
    fields = lp_v4_seed.build_fields(p, _rpc, source="owner")
    if V.pool_state(_rpc, "robinhood", fields["pool_id"]).sqrt_price_x96:
        pytest.skip("the test key is already initialised on this fork")
    ref, f = S.propose("owner", fields, origin="owner_seat")
    S.confirm(ref, user_id="owner")
    monkeypatch.setattr(lp_v4_seed, "_uid", lambda ctx: "owner")
    run = SimpleNamespace(chain="robinhood", seed=ref, dry_run=False, max_spend_usd=300.0)
    return ref, f, run


def _allow_permit2(amount=10 ** 26):
    _send(TREASURY, TOKEN, "0x095ea7b3" + ROW.permit2[2:].lower().rjust(64, "0")
          + format(amount, "064x"))
    _send(TREASURY, ROW.permit2, _grant(amount)[1]["data"])


def test_a_declared_pool_is_seeded_in_one_transaction_and_reads_back(fork, monkeypatch):
    from core.wallet import liquidity_guard, lp_seed as S
    from tools.defi import lp_v4_seed
    from tools.defi.lp_verbs import receipt_position
    ref, f, run = _declared(fork, monkeypatch)
    _allow_permit2()
    plan = lp_v4_seed.prepare_seed_v4(run, _rpc, TREASURY, ROW.position_manager,
                                      lp_v4_seed.load(run, None))
    assert plan.seed["creates"]
    tx = {"to": ROW.position_manager, "from": TREASURY, "value": plan.value,
          "data": plan.data, "chainId": 4663}
    next_id = int(lp_reads.view(_rpc, ROW.position_manager, V.POSM_NEXT_TOKEN_ID))

    d = _authorize(plan.intent, tx, fork["price"])
    assert d.allowed and d.position_token_id == next_id, d.reason
    # the token has NO independent price: the guard values it through the signed price
    eth_only = lambda chain, addr: ETH_USD if addr.lower() != TOKEN.lower() else None
    d2 = _authorize(plan.intent, tx, eth_only)
    assert d2.allowed and d2.amount_usd > 0, d2.reason
    # a bare initializePool is refused: it would leave an empty pool at a free price
    from core.wallet import abi
    bare = abi.encode_call(V.POSM_INITIALIZE_POOL["name"], V.POSM_INITIALIZE_POOL["inputs"],
                           [V._key_tuple(S.key_of(f)), int(f["sqrt_price_x96"])])
    d3 = _authorize(plan.intent, dict(tx, data=bare, value=0), fork["price"])
    assert not d3.allowed and "initializePool" in d3.reason, d3.reason

    # not vacuous: the same bytes create the pool and mint the predicted id
    lp_v4_seed.claim(plan.seed)
    rc = _send(TREASURY, ROW.position_manager, plan.data, value=plan.value)
    token_id = receipt_position(plan, rc, TREASURY)
    assert token_id == next_id
    liquidity_guard.assert_position_events(plan.intent, rc["logs"], token_id, {"data": plan.data})
    text = lp_v4_seed.settle(plan.seed, _rpc, "robinhood", tx_hash=rc["transactionHash"],
                             holder=TREASURY, token_id=token_id, reverted=False)
    assert "SEEDED" in text and "MISMATCH" not in text and "UNVERIFIED" not in text, text
    assert S.get(ref, user_id="owner")["state"] == S.SEEDED

    # the reads (W5/W6) see the new pool and the position
    lines = "\n".join(V.position_lines(_rpc, "robinhood", token_id, lambda t: 18, holder=TREASURY))
    assert f["pool_id"] in lines and "(this wallet)" in lines and "in range" in lines
    info = "\n".join(V.pool_info_lines(_rpc, "robinhood", "native", TOKEN, fee=SEED_FEE,
                                       tick_spacing=SEED_SPACING, decimals_fn=lambda t: 18))
    assert f["pool_id"] in info and "NOT initialized" not in info

    # a second declaration on the pool that now exists is a plain mint (no initialise)
    ref2, _, run2 = _declared_again(f, monkeypatch)
    plan2 = lp_v4_seed.prepare_seed_v4(run2, _rpc, TREASURY, ROW.position_manager,
                                       lp_v4_seed.load(run2, None))
    assert not plan2.seed["creates"]
    d4 = _authorize(plan2.intent, dict(tx, data=plan2.data, value=plan2.value), fork["price"])
    assert d4.allowed, d4.reason

    # the withdrawal (W7): half of the position, the NFT stays
    from tools.defi.lp_v4_verbs import prepare_remove_v4
    rm = SimpleNamespace(chain="robinhood", token_id=token_id, liquidity_pct=50, slippage_bps=100,
                         burn=False, dry_run=False, max_spend_usd=5.0)
    out = prepare_remove_v4(rm, _rpc, TREASURY, ROW.position_manager, price_fn=fork["price"])
    rtx = {"to": ROW.position_manager, "from": TREASURY, "value": 0, "data": out.data,
           "chainId": 4663, "maxFeePerGas": 10 ** 9}   # an exit is valued by its fee alone
    d5 = _authorize(out.intent, rtx, fork["price"])
    assert d5.allowed, d5.reason
    before = V.position(_rpc, "robinhood", token_id).liquidity
    rc2 = _send(TREASURY, ROW.position_manager, out.data)
    liquidity_guard.assert_position_events(out.intent, rc2["logs"], token_id, {"data": out.data})
    after = V.position(_rpc, "robinhood", token_id)
    assert after.liquidity == before - before * 50 // 100 and after.owner == TREASURY.lower()


def _declared_again(f, monkeypatch):
    from core.wallet import lp_seed as S
    fields = {k: f[k] for k in S.FIELDS}
    ref, f2 = S.propose("owner", fields, origin="owner_seat")
    S.confirm(ref, user_id="owner")
    return ref, f2, SimpleNamespace(chain="robinhood", seed=ref, dry_run=False, max_spend_usd=300.0)


# ---- the statistics are the chain's own numbers --------------------------------

#: A live v4 position to check the reads against (its owner is impersonated on the
#: fork). From the env, never pinned here.
LIVE_POSITION = os.environ.get("LP_V4_FORK_POSITION", "")


def _balances(owner, key):
    native = int(_rpc("eth_getBalance", [owner, "latest"]), 16)
    token = int(lp_reads.view(_rpc, key["currency1"], {"name": "balanceOf",
                "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]}, [owner]))
    return native, token


def _impersonated(owner, to, data):
    """Send with a zero gas price, so a native balance delta is the receipt alone."""
    _rpc("anvil_impersonateAccount", [owner])
    _rpc("anvil_setNextBlockBaseFeePerGas", ["0x0"])
    h = _rpc("eth_sendTransaction", [{"from": owner, "to": to, "data": data, "value": "0x0",
                                      "gas": hex(2_000_000), "gasPrice": "0x0"}])
    for _ in range(50):
        rc = _rpc("eth_getTransactionReceipt", [h])
        if rc:
            assert int(rc["status"], 16) == 1, rc
            return rc
        time.sleep(0.1)
    raise AssertionError("no receipt")


@pytest.mark.skipif(not LIVE_POSITION, reason="set LP_V4_FORK_POSITION=<a live v4 position id>")
def test_fees_owed_and_holdings_equal_what_a_collect_and_a_withdrawal_pay(fork):
    token_id = int(LIVE_POSITION)
    pos = V.position(_rpc, "robinhood", token_id)
    assert pos.key["currency0"] == V.ZERO, "the check assumes a native currency0"
    st = V.pool_state(_rpc, "robinhood", pos.pool_id)
    fees = V.position_fees(_rpc, "robinhood", pos)
    from core.wallet import univ3_math as M
    holds = M.amounts_for_liquidity(st.sqrt_price_x96, M.sqrt_price_at_tick(pos.tick_lower),
                                    M.sqrt_price_at_tick(pos.tick_upper), pos.liquidity)

    # 1. the fees read == what a collect pays, to the wei
    before = _balances(pos.owner, pos.key)
    unlock = V.encode_collect_unlock(token_id, pos.key["currency0"], pos.key["currency1"], pos.owner)
    _impersonated(pos.owner, ROW.position_manager,
                  V.encode_modify_liquidities(unlock, int(time.time()) + 600))
    after = _balances(pos.owner, pos.key)
    paid = (after[0] - before[0], after[1] - before[1])
    assert paid == fees, (paid, fees)
    assert V.position_fees(_rpc, "robinhood", V.position(_rpc, "robinhood", token_id)) == (0, 0)

    # 2. "holds now" == what a full withdrawal pays (the pool rounds down: at most 1 wei less)
    unlock = V.encode_decrease_unlock(token_id, pos.liquidity, 0, 0, pos.key["currency0"],
                                      pos.key["currency1"], pos.owner)
    _impersonated(pos.owner, ROW.position_manager,
                  V.encode_modify_liquidities(unlock, int(time.time()) + 600))
    final = _balances(pos.owner, pos.key)
    out = (final[0] - after[0], final[1] - after[1])
    for got, want in zip(out, holds):
        assert 0 <= want - got <= 1, (out, holds)

    # 3. the LP part of every Swap log since the mint == the fees (when this
    #    position was the pool's only liquidity the whole time)
    act = V.pool_activity(_rpc, "robinhood", pos.pool_id, hours=72, protocol_fee=st.protocol_fee)
    if st.liquidity == pos.liquidity:
        for summed, owed in zip((act["lp_fees0"], act["lp_fees1"]), fees):
            assert abs(summed - owed) <= max(act["swaps"] * 2, owed // 10_000), (act, fees)


# ---- 118: manage a live position — increase it, then move part of it ---------

def _live(fork_state=None):
    pos = V.position(_rpc, "robinhood", int(LIVE_POSITION))
    if pos.owner != TREASURY.lower() or pos.key["currency0"] != V.ZERO:
        pytest.skip("LP_V4_FORK_POSITION must be a native-currency0 position LP_V4_FORK_HOLDER owns")
    st = V.pool_state(_rpc, "robinhood", pos.pool_id)
    token = pos.key["currency1"]
    ratio = (st.sqrt_price_x96 / 2 ** 96) ** 2
    price = lambda chain, addr: ETH_USD / ratio if str(addr).lower() == token else ETH_USD
    return pos, st, token, ratio, price


def _allow(token, amount=10 ** 27):
    _send(TREASURY, token, "0x095ea7b3" + ROW.permit2[2:].lower().rjust(64, "0")
          + format(amount, "064x"))
    _send(TREASURY, ROW.permit2,
          V.encode_permit2_approve(token, ROW.position_manager, amount, int(time.time()) + 900))


def _tx_of(plan):
    return {"to": ROW.position_manager, "from": TREASURY, "value": plan.value,
            "data": plan.data, "chainId": 4663}


@pytest.mark.skipif(not LIVE_POSITION, reason="set LP_V4_FORK_POSITION=<a live v4 position id>")
def test_an_increase_passes_the_guard_and_adds_exactly_the_signed_liquidity(fork):
    """118 W6: INCREASE_LIQUIDITY + SETTLE_PAIR + SWEEP on a position the wallet holds."""
    import dataclasses
    from core.wallet import liquidity_guard as G
    from tools.defi.lp_v4_verbs import prepare_increase_v4
    pos, st, token, ratio, price = _live()
    _allow(token)
    p = SimpleNamespace(chain="robinhood", protocol="v4", token_id=pos.token_id,
                        token_a="native", token_b=token, amount_a=0.01, amount_b=0.02 * ratio,
                        initial_price=None, tick_spacing=None, slippage_bps=100,
                        max_spend_usd=200.0, dry_run=True)
    plan = prepare_increase_v4(p, _rpc, TREASURY, ROW.position_manager, price_fn=price)
    tx = _tx_of(plan)
    d = _authorize(plan.intent, tx, price)
    assert d.allowed, d.reason
    assert d.amount_usd and d.amount_usd < 200.0
    # another position id under the same bytes refuses
    other = dataclasses.replace(plan.intent, lp_position=(ROW.position_manager, pos.token_id + 1))
    d2 = _authorize(other, tx, price)
    assert not d2.allowed, d2.reason
    signed = G.decode_v4_increase(tx)["liquidity"]
    native_before = int(_rpc("eth_getBalance", [TREASURY, "latest"]), 16)
    rc = _send(TREASURY, ROW.position_manager, plan.data, value=plan.value)
    assert V.position(_rpc, "robinhood", pos.token_id).liquidity == pos.liquidity + signed
    G.assert_position_events(plan.intent, rc["logs"], pos.token_id, tx)
    # the SWEEP returned the native the increase did not use
    spent = native_before - int(_rpc("eth_getBalance", [TREASURY, "latest"]), 16)
    gas = int(rc["gasUsed"], 16) * int(rc["effectiveGasPrice"], 16)
    assert 0 < spent - gas <= plan.value


@pytest.mark.skipif(not LIVE_POSITION, reason="set LP_V4_FORK_POSITION=<a live v4 position id>")
def test_a_move_withdraws_then_mints_the_new_range_and_the_step_reads_from_the_chain(fork, monkeypatch):
    """118 W7: the declaration, the existing withdrawal, the existing mint."""
    from core.wallet import lp_seed as S
    from tools.defi import lp_v4_move, lp_v4_seed
    from tools.defi.lp_v4_verbs import prepare_remove_v4
    from tools.defi.lp_verbs import ticks
    pos, st, token, ratio, price = _live()
    spacing = pos.key["tickSpacing"]
    lo, hi = (st.tick - 6000) // spacing * spacing, (st.tick + 6000) // spacing * spacing
    p = SimpleNamespace(chain="robinhood", protocol="v4", move_from=pos.token_id, move_pct=50,
                        range=f"ticks:{lo},{hi}", slippage_bps=300)
    fields = lp_v4_move.build_fields(p, _rpc, TREASURY, ticks=ticks, symbol=lp_v4_seed._symbol)
    ref, f = S.propose("owner", fields, origin="owner_seat")
    S.confirm(ref, user_id="owner")
    assert S.summary_lines(f)[0].startswith("MOVE: first withdraw 50%")
    assert lp_v4_move.stage(f, pos.liquidity) == "withdraw"

    # The mint is judged BEFORE the withdrawal, against what the withdrawal
    # returns: with the grants in place it passes; without the cap it refuses.
    from tools.defi import lp_verbs as LV
    from tools.defi.trade_params import LpAddParams
    _allow(token)
    monkeypatch.setattr(lp_v4_seed, "_uid", lambda ctx: "owner")
    monkeypatch.setenv("LP_ETH_FLOOR", "0")
    live = LpAddParams(chain="robinhood", protocol="v4", seed=ref, dry_run=False,
                       max_spend_usd=5000.0)
    check = lambda: LV._move_precheck(
        SimpleNamespace(_price=price), live, S.get(ref, user_id="owner"), _rpc, TREASURY,
        ROW.position_manager, PolicyGate(max_per_tx_usd=50000.0, daily_cap_usd=50000.0), pos)
    monkeypatch.setenv("LP_ETH_CAP", "0")
    assert "LP_ETH_CAP is 0" in check()
    monkeypatch.setenv("LP_ETH_CAP", "100")
    assert check() is None, check()
    assert S.claim_withdrawal(ref) and not S.claim_withdrawal(ref)      # taken once

    # step 1: the existing withdrawal shape
    rp = SimpleNamespace(chain="robinhood", protocol="v4", token_id=pos.token_id,
                         liquidity_pct=50, slippage_bps=300, max_spend_usd=20.0, burn=False,
                         dry_run=True)
    out = prepare_remove_v4(rp, _rpc, TREASURY, ROW.position_manager, price_fn=price)
    d = _authorize(out.intent, dict(_tx_of(out), maxFeePerGas=10 ** 9), price)   # valued by its fee
    assert d.allowed, d.reason
    _send(TREASURY, ROW.position_manager, out.data)
    left = V.position(_rpc, "robinhood", pos.token_id).liquidity
    assert S.set_withdrawal(ref, "0x" + "ab" * 32)
    mark = S.get(ref, user_id="owner")["tx_hash"]
    assert lp_v4_move.stage(f, left, mark) == "mint"
    with pytest.raises(ValueError, match="The position changed"):
        lp_v4_move.stage(f, left)                       # no mark: not this move's doing

    # step 2: the existing mint shape, from the declaration
    run = SimpleNamespace(chain="robinhood", seed=ref, dry_run=False, max_spend_usd=5000.0)
    plan = lp_v4_seed.prepare_seed_v4(run, _rpc, TREASURY, ROW.position_manager,
                                      lp_v4_seed.load(run, None))
    next_id = int(lp_reads.view(_rpc, ROW.position_manager, V.POSM_NEXT_TOKEN_ID))
    d = tx_guard.authorize(
        plan.intent, _tx_of(plan), holder=TREASURY,
        gate=PolicyGate(max_per_tx_usd=50000.0, daily_cap_usd=50000.0), execution_context=None,
        price_fn=price, rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False, liquidity_rpc=_rpc)
    assert d.position_token_id == next_id, d.reason
    _send(TREASURY, ROW.position_manager, plan.data, value=plan.value)
    new = V.position(_rpc, "robinhood", next_id)
    assert (new.owner, new.tick_lower, new.tick_upper) == (TREASURY.lower(), lo, hi)
    assert new.liquidity == plan.seed["liquidity"] > 0
    assert V.position(_rpc, "robinhood", pos.token_id).liquidity == left
