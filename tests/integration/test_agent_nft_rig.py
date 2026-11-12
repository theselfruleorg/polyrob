"""The core money guard against the CURRENT agent-NFT contracts (proposal 115 W2), on the rig of
``tests/integration/agent_nft_rig.py`` (a plain local anvil, chain id 46630, the desk's own
``Deploy.s.sol`` and ``pin.py``; no fork, no live chain, fresh keys only).

Run (opt-in; skipped with a named reason otherwise)::

    AGENT_NFT_RIG=1 python -m pytest tests/integration/test_agent_nft_rig.py -s -p no:warnings

``AGENT_NFT_RIG=<path>`` names another Foundry checkout of the collection.

The tests share ONE chain, in file order (a module fixture): each step starts from the state the
one before it left. Every money write goes through ``tx_guard.authorize`` (structural rules +
``eth_simulateV1`` + the caps), is signed by a fresh ``LocalEoaSigner`` and broadcast by
``EvmRail`` — the path every core verb takes.
"""
from __future__ import annotations

import asyncio
import os
import types

import pytest

from core.wallet import account_kinds
from tests.integration import agent_nft_rig as R

pytestmark = pytest.mark.skipif(R.skip_reason() is not None, reason=R.skip_reason() or "")

ETH_USD = 4000.0


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Wallet:
    def __init__(self, signer, gate):
        self._signer, self.policy = signer, gate

    def operational_signer(self):
        return self._signer


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    from eth_account import Account

    from core.wallet import collection_registry
    from core.wallet.audit_sink import JsonlAuditSink
    from core.wallet.policy import PolicyGate
    from core.wallet.signer import LocalEoaSigner

    work = tmp_path_factory.mktemp("agent_nft_rig")
    mp = pytest.MonkeyPatch()
    r = R.start(work)
    try:
        home = work / "home"
        mp.setenv("POLYROB_DATA_DIR", str(home))
        mp.setenv("DEFI_EVM_RPC_ROBINHOOD_TESTNET", r.core_url)
        mp.setenv("VALUELESS_CHAIN_MONEY", "true")
        mp.setenv("AGENT_NFT_ENABLED", "true")
        for k in ("AUTONOMY_HALT", "DATA_ROOT", "POLYROB_INSTANCE_ID", "DEFI_AUTONOMOUS_MAX_USD",
                  "AGENT_WALLET_MAX_PER_TX_USD", "AGENT_NFT_REVEAL_MAX_GAS_USD", "WALLET_SIGNER"):
            mp.delenv(k, raising=False)
        registry = R.write_registry(r, work / "agent_nft_collections.json")
        mp.setattr(collection_registry, "REGISTRY_FILE", str(registry))
        treasury = Account.create()
        r.fund(treasury.address, 1000 * 10 ** 18)
        assert r.code(treasury.address) in ("0x", "0x0")
        signer = LocalEoaSigner(bytes(treasury.key))
        gate = PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0,
                          audit_sink=JsonlAuditSink(str(work / "wallet_audit.jsonl")))
        ns = types.SimpleNamespace(r=r, home=home, work=work, treasury=treasury, signer=signer,
                                   gate=gate, wallet=_Wallet(signer, gate), registry=registry,
                                   told=[], minted=[], mp=mp)
        print(f"\nrig: anvil {r.url}, collection {r.collection}, sale {r.sale}, account impl "
              f"{r.account_impl}, journal {r.journal_log}, deploy block {r.deploy_block}")
        yield ns
    finally:
        mp.undo()
        r.stop()


# ---------------------------------------------------------------------------------- the guard

def _authorize(s, intent, tx):
    from core.wallet import tx_guard
    return tx_guard.authorize(intent, tx, holder=s.signer.address, gate=s.gate,
                              execution_context=None, price_fn=lambda c, a: ETH_USD,
                              account_rpc=s.r.core_rpc)


def _mint_tx(s, *, kind="mint", qty=1, value=None, to=None, target=None, allowance=0, proof=(),
             ids=None, key=None):
    """``(intent, tx)`` of a sale mint as core's mint shape declares it."""
    from core.wallet import collection_mint as M
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet.tx_guard import TxIntent
    to = to or s.signer.address
    target = target or s.r.sale
    data = (M.encode_mint_wl(to, qty, 0, allowance, list(proof)) if kind == "mintWL"
            else M.encode_mint(to, qty, 0))
    value = qty * M.BASE_WEI if value is None else value
    first = s.r.next_id()
    ids = tuple(range(first, first + qty)) if ids is None else tuple(ids)
    rail = EvmRail(chain=R.CHAIN, signer=s.signer)
    tx = rail.build_call(to=target, data=data, value=value)
    intent = TxIntent(chain=R.CHAIN, token=None, to=target, amount_raw=value, max_spend_usd=50.0,
                      idempotency_key=key or f"rig-mint-{kind}-{first}-{qty}-{value}-{os.urandom(4).hex()}",
                      is_collection_mint=True, mint_ids=ids)
    return intent, tx, rail


def _send(s, intent, tx, rail, decision, *, action):
    signed = rail.size_gas(tx, decision.sim_gas_used)
    h = rail.sign_and_send(signed)
    rc = rail.await_receipt(h)
    assert rc.status == "success", rc
    s.gate.record(venue="defi", action=action, amount_usd=decision.amount_usd or 0.0,
                  counterparty=intent.to, idempotency_key=intent.idempotency_key, result_ref=h,
                  chain=R.CHAIN)
    return h, rc


def _guarded_mint(s, **kw):
    intent, tx, rail = _mint_tx(s, **kw)
    d = _authorize(s, intent, tx)
    assert d.allowed, d.reason
    bal0 = s.r.balance(s.signer.address)
    h, rc = _send(s, intent, tx, rail, d, action="agent_nft_collection_mint")
    paid = bal0 - s.r.balance(s.signer.address)
    assert intent.amount_raw <= paid < intent.amount_raw + 10 ** 15, (paid, intent.amount_raw)
    for i in intent.mint_ids:
        assert s.r.owner_of(i) == s.signer.address.lower(), i
    s.minted.extend(intent.mint_ids)
    print(f"  mint {kw.get('kind', 'mint')} ids {list(intent.mint_ids)} value {intent.amount_raw}: "
          f"{d.reason} (lane {d.lane}, sim gas {d.sim_gas_used}, gas {rc.gas_used})")
    return intent, d


def _refused(s, why_part, **kw):
    intent, tx, _rail = _mint_tx(s, **kw)
    d = _authorize(s, intent, tx)
    assert not d.allowed, f"the guard ALLOWED {kw}: {d.reason}"
    if why_part:
        assert why_part in d.reason, d.reason
    print(f"  refused {kw}: {d.reason[:200]}")
    return d


# ---------------------------------------------------------------------------------- the tests

def test_01_pin_py_profile_is_the_pinned_registry(rig):
    """pin.py (unchanged) verified every on-chain pin and THIS core's parser accepted the profile;
    the root registry file loads it with the sale and the PolyrobAccount version."""
    from core.wallet import collection_registry as CR
    r = rig.r
    assert "validated by core collection_registry.parse" in r.pin_log, r.pin_log
    (p,) = CR.load()
    assert p.chain_id == R.CHAIN_ID and p.address == r.collection
    assert p.sale and p.sale.address == r.sale and p.sale.max_price_wei == R.START_PRICE
    assert p.accounts[0].kind is account_kinds.ERC6551P and p.accounts[0].implementation == r.account_impl
    assert p.erc6551p.market == r.market and p.erc6551p.zone == r.zone
    assert p.journal_log == r.journal_log
    assert CR.runtime_refusal(r.rpc, p) is None and CR.sale_runtime_refusal(r.rpc, p) is None
    # the pinned constants of the mint shape ARE the sale's
    from core.wallet import collection_mint as M
    assert r.uint(r.sale, "BASE()") == M.BASE_WEI
    assert r.uint(r.sale, "START_PRICE()") == p.sale.max_price_wei


def test_02_whitelist_mint_through_the_guard(rig):
    """Phase 1: ``mintWL`` with a real Merkle proof (the desk's tree) is allowed and lands; a
    mint addressed to the collection, a wrong ``to``, a value off qty × BASE, an allowance below
    qty and a proof for another leaf are refused."""
    from eth_account import Account
    s, r = rig, rig.r
    wl = R.wl_tree(r.contracts)
    others = [Account.create().address for _ in range(4)]
    entries = [{"address": s.signer.address, "allowance": 3}] + [
        {"address": a, "allowance": 2} for a in others]
    root, tree, index = wl.build(entries)
    proof = wl.proof(tree, index, s.signer.address)
    assert wl.verify(root, s.signer.address, 3, proof) and len(proof) >= 2
    r.send(r.owner, r.sale, r.encode("configure(bytes32)", bytes.fromhex(root[2:])))
    r.send(r.owner, r.collection, r.encode("openMint()"))
    assert r.phase() == 1

    print()
    _refused(s, "itself", kind="mintWL", qty=1, allowance=3, proof=proof, target=r.collection)
    _refused(s, "not to the signing treasury", kind="mintWL", qty=1, allowance=3, proof=proof,
             to=others[0])
    _refused(s, "exactly", kind="mintWL", qty=1, allowance=3, proof=proof, value=R.BASE - 1)
    _refused(s, "exactly", kind="mintWL", qty=1, allowance=3, proof=proof, value=R.BASE + 1)
    _refused(s, "allowance", kind="mintWL", qty=2, allowance=1, proof=proof)
    # a proof for a leaf that is not in the tree: structurally fine, the SALE reverts NotListed
    _refused(s, None, kind="mintWL", qty=1, allowance=2, proof=proof)
    # the public mint in the whitelist phase: the sale reverts NotPublicPhase in the simulation
    _refused(s, None, kind="mint", qty=1, value=R.START_PRICE)

    _guarded_mint(s, kind="mintWL", qty=2, allowance=3, proof=proof)
    _guarded_mint(s, kind="mintWL", qty=1, allowance=3, proof=proof)
    # the allowance is used up: the sale reverts OverAllowance in the simulation
    _refused(s, None, kind="mintWL", qty=1, allowance=3, proof=proof)
    assert s.minted == [1, 2, 3]
    s.proof = proof


def _sim_revert_data(s, intent, tx):
    """The revert data eth_simulateV1 reports for *tx* (what the guard's simulation reads)."""
    r = s.r
    res = r.core_rpc("eth_simulateV1", [{"blockStateCalls": [{"calls": [{
        "from": s.signer.address, "to": tx["to"], "data": tx["data"], "value": hex(tx["value"]),
        "gas": hex(10_000_000)}]}], "validation": False}, "latest"])
    entry = res[0]["calls"][0]
    err = entry.get("error") if isinstance(entry.get("error"), dict) else {}
    return str(err.get("data") or entry.get("returnData") or "")


def test_02b_the_sale_revert_reaches_the_simulation(rig):
    """The simulation the guard runs carries the SALE's custom error in its revert data
    (``NotListed()`` for a proof of a leaf not in the tree): the name is there to read."""
    s = rig
    intent, tx, _rail = _mint_tx(s, kind="mintWL", qty=1, allowance=2, proof=s.proof)
    data = _sim_revert_data(s, intent, tx)
    assert data.lower().startswith(R.Rig.encode("NotListed()").lower()), data


def test_02c_a_sale_revert_is_named_in_the_mint_refusal(rig):
    s = rig
    d = _refused(s, None, kind="mintWL", qty=1, allowance=2, proof=s.proof)
    assert "NotListed" in d.reason, d.reason


def test_03_auction_mint_at_and_above_the_live_price(rig):
    """Phase 2: ``mint`` at ``priceNow()`` and at the profile's maximum is allowed; below the
    live price (the sale reverts), below BASE and above the pinned maximum are refused."""
    s, r = rig, rig.r
    r.warp(R.WL_DURATION + 3 * R.STEP_SECONDS + 5)
    assert r.phase() == 2
    p = r.price_now()
    assert R.BASE < p < R.START_PRICE, p
    print(f"\n  auction price now {p}")
    _refused(s, "at most", kind="mint", qty=1, value=R.START_PRICE + 1)
    _refused(s, "at least", kind="mint", qty=1, value=R.BASE - 1)
    _refused(s, None, kind="mint", qty=1, value=p - 1)            # WrongPrice in the simulation
    _refused(s, "itself", kind="mint", qty=1, value=p, target=r.collection)
    _refused(s, "not to the signing treasury", kind="mint", qty=1, value=p,
             to="0x000000000000000000000000000000000000dEaD")
    # declaring an id the mint does not create: the simulation delivers another id
    _refused(s, "exactly ids", kind="mint", qty=1, value=p, ids=(r.next_id() + 5,))
    _guarded_mint(s, kind="mint", qty=1, value=p)
    _guarded_mint(s, kind="mint", qty=2, value=2 * R.START_PRICE)  # above the live price
    assert r.uint(r.sale, "records()") == 2


def test_04_mint_after_the_settlement_at_base(rig):
    """Phase 3 (settled): ``mint`` at exactly qty × BASE is allowed; BASE + 1 passes the
    structural bound (it is under the pinned maximum) and is refused by the SIMULATION
    (the sale reverts WrongPrice)."""
    s, r = rig, rig.r
    r.warp(R.STEPS * R.STEP_SECONDS)
    assert r.phase() == 3 and r.price_now() == R.BASE
    print()
    _refused(s, None, kind="mint", qty=1, value=R.BASE + 1)
    # W8: no intent but the mint shape may pay the pinned sale (a plain native send to it)
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet.tx_guard import TxIntent
    rail = EvmRail(chain=R.CHAIN, signer=s.signer)
    tx = rail.build_native_transfer(to=r.sale, amount_wei=R.BASE)
    d = _authorize(s, TxIntent(chain=R.CHAIN, token=None, to=r.sale, amount_raw=R.BASE,
                               max_spend_usd=50.0, idempotency_key="rig-pay-sale", is_transfer=True), tx)
    print(f"  native send to the sale: {d.reason[:160]}")
    assert not d.allowed and "pinned sale" in d.reason, d.reason
    _guarded_mint(s, kind="mint", qty=3, value=3 * R.BASE)
    # settle the two auction records (anyone may): the excess comes back to the treasury
    before = r.balance(s.signer.address)
    settler = "0x00000000000000000000000000000000000c0FFe"
    r.fund(settler)
    r.send(settler, r.sale, r.encode("settle(uint256[])", [0, 1]))
    refund = r.balance(s.signer.address) - before
    assert refund > 0, refund
    print(f"  settle: the treasury got {refund} wei back (paid above the BASE clearing)")


def test_05_qty_10_mint_refuses_at_sizing_and_the_largest_that_fits_lands(rig):
    """The sale's per-call maximum (MAX_PER_TX = 10) through the guard and the rail (115 R1). The
    collection reverts ``IdentityGas`` unless 800,000 × 64/63 + 10,000 gas is LEFT before each
    id's identity call, so a mint needs more than it uses; ×1.5 capped at MAX_GAS_LIMIT left a
    10-id mint 6.8% over its gas used. The rail now refuses a cap that leaves under ×1.2 of the
    measurement and names the remedy; the largest quantity that keeps ×1.2 lands with every
    identity registered."""
    from core.wallet.broadcast.evm import MAX_GAS_LIMIT, GasCeilingExceeded
    s, r = rig, rig.r
    intent, tx, rail = _mint_tx(s, kind="mint", qty=10, value=10 * R.BASE)
    d = _authorize(s, intent, tx)
    assert d.allowed, d.reason
    need10 = rail.estimate_gas(tx)
    with pytest.raises(GasCeilingExceeded) as exc:
        rail.size_gas(tx, d.sim_gas_used)
    print(f"\n  qty 10: guard {d.reason}, sim gas {d.sim_gas_used}, eth_estimateGas {need10} "
          f"(rail MAX_GAS_LIMIT {MAX_GAS_LIMIT}); size_gas: {exc.value}")
    assert "Split the call" in str(exc.value) and "fewer ids per mint" in str(exc.value)
    assert d.sim_gas_used * 6 // 5 > MAX_GAS_LIMIT

    for qty in range(9, 0, -1):
        intent, tx, rail = _mint_tx(s, kind="mint", qty=qty, value=qty * R.BASE)
        d = _authorize(s, intent, tx)
        assert d.allowed, d.reason
        try:
            signed = rail.size_gas(tx, d.sim_gas_used)
            break
        except GasCeilingExceeded as e:
            print(f"  qty {qty}: sim gas {d.sim_gas_used} — refused at sizing too ({str(e)[:90]}…)")
    need = rail.estimate_gas(tx)
    print(f"  largest qty that sizes: {qty} — sim gas {d.sim_gas_used}, eth_estimateGas {need}, "
          f"limit {signed['gas']} (x{signed['gas'] / d.sim_gas_used:.3f} of the simulation)")
    assert qty >= 8 and signed["gas"] >= d.sim_gas_used * 6 // 5
    # between the simulation and the inclusion (an owner approval takes time) other buyers mint
    # and their ids fall due: the treasury's mint then also auto-reveals up to 4 of them
    buyer = "0x00000000000000000000000000000000000b0b01"
    r.fund(buyer)
    r.send(buyer, r.sale, r.encode("mint(address,uint256,uint256)", buyer, 10, 0), value=10 * R.BASE)
    r.mine(3)
    intent = __import__("dataclasses").replace(
        intent, mint_ids=tuple(range(r.next_id(), r.next_id() + qty)))
    h = rail.sign_and_send(signed)
    rc = rail.await_receipt(h)
    print(f"  qty {qty}: status {rc.status}, gas used {rc.gas_used} (simulated {d.sim_gas_used}, "
          f"limit {signed['gas']}: headroom {signed['gas'] - rc.gas_used} gas, "
          f"{(signed['gas'] - rc.gas_used) / rc.gas_used:.1%} over the gas used, "
          f"{(signed['gas'] - need) / need:.1%} over the estimate before the burst)")
    assert rc.status == "success", (rc, signed["gas"], d.sim_gas_used)
    s.gate.record(venue="defi", action="agent_nft_collection_mint", amount_usd=0.0,
                  counterparty=intent.to, idempotency_key=intent.idempotency_key, result_ref=h,
                  chain=R.CHAIN)
    skipped = [i for i in intent.mint_ids if not r.uint(r.collection, "hasIdentity(uint256)", i)]
    assert not skipped, f"ids minted WITHOUT an identity (short of gas): {skipped}"
    s.minted.extend(intent.mint_ids)


def test_06_hold_watch_inherited_and_view(rig):
    """Hold: the holdings watch reports every minted id once and records, once and in token
    units, what an account ALREADY held (W1); the account view reads the PolyrobAccount kind (the
    pinned clone), ``saleReadyOf``, a complete empty approval table, the identity the mint
    registered (``hasIdentity`` / ``agentIdOf``, owned by the account) and the reputation line."""
    from core.instance import resolve_owner_user_id
    from core.open_positions import get_position
    from core.wallet import collection_registry as CR
    from core.wallet import erc8004_reputation as REP
    from core.wallet import nft_holdings
    from tools.agent_nft import view as V
    s, r = rig, rig.r
    acc1 = r.account_of(1)
    # the account of #1 already holds 5 WETH (the chain's pinned wrapped native) before the watch
    r.fund("0x000000000000000000000000000000000000beef")
    r.send("0x000000000000000000000000000000000000beef", R.WETH_TESTNET,
           r.encode("mint(address,uint256)", acc1, 5 * 10 ** 18))

    async def notify(_c, _u, text):
        s.told.append(text)

    def tick():
        return _run(nft_holdings.tick(None, treasury=s.signer.address,
                                      rpc_for=lambda c: r.core_rpc, notify=notify))
    res = tick()
    print("\n  watch:", res.as_dict(), res.errors)
    assert not res.errors, res.errors
    assert sorted(x["token_id"] for x in res.arrived) == sorted(s.minted)
    first = next(t for t in s.told if "POLYROB #1 " in t)
    print("  notice #1:", first)
    assert "already holds WETH" in first and "inherited" in first, first
    again = tick()
    assert again.arrived == [] and again.lost == [] and again.kept == len(s.minted)
    pos = get_position(resolve_owner_user_id(), R.CHAIN, R.WETH_TESTNET, account=acc1)
    assert pos is not None and pos.origin == "inherited" and pos.qty == 5.0 and pos.entry_usd is None, pos

    (profile,) = CR.load()
    v = V.build_view(r.core_rpc, R.CHAIN, profile, 1, treasury=s.signer.address)
    print("  " + V.render(v).replace("\n", "\n  "))
    assert v.kind is account_kinds.ERC6551P and v.account.lower() == acc1.lower()
    assert v.implementation_ok is True and v.owned_by_treasury is True
    assert v.sale_ready is False and v.approvals == [] and v.coverage_complete
    assert v.identity_count == 1, v.identity_count
    agent_id = REP.agent_id_of(r.core_rpc, r.collection, 1)
    assert agent_id is not None and agent_id >= 1
    assert r.address_of(R.IDENTITY_TESTNET, "ownerOf(uint256)", agent_id).lower() == acc1.lower()
    assert isinstance(v.reputation, REP.Reputation) and v.reputation.agent_id == agent_id
    assert v.reputation.clients == () and "no feedback" in v.reputation.line()
    assert v.native_wei not in (V.UNREADABLE, 0)


def test_07_reputation_read(rig):
    """W5: two clients give feedback on the agentId of #1 (the ERC-8004 v1 Reputation ABI, a
    stand-in contract); the view's reputation line names the count, the sum and the clients."""
    from core.wallet import collection_registry as CR
    from core.wallet import erc8004_reputation as REP
    from tools.agent_nft import view as V
    s, r = rig, rig.r
    agent_id = REP.agent_id_of(r.core_rpc, r.collection, 1)
    clients = ["0x00000000000000000000000000000000000c1001", "0x00000000000000000000000000000000000c1002"]
    for c, score in zip(clients, (80, 60)):
        r.fund(c)
        r.send(c, R.REPUTATION_TESTNET, r.encode(
            "giveFeedback(uint256,int128,uint8,string,string,string,string,bytes32)",
            agent_id, score, 0, "", "", "", "", b"\0" * 32))
    (profile,) = CR.load()
    v = V.build_view(r.core_rpc, R.CHAIN, profile, 1, treasury=s.signer.address)
    print("\n  reputation:", v.reputation.line())
    assert v.reputation.count == 2 and v.reputation.value == 140
    assert sorted(c.lower() for c in v.reputation.clients) == clients


def test_08_act_from_the_account_native_and_erc20(rig, monkeypatch):
    """The account acts: ``defi_trade.transfer`` with ``nft=`` sends native and the inherited
    ERC-20 OUT of the account of #1 through ``TxIntent.via_account``; with the pinned
    ``journal_log`` the signed journal entry rides the same ``executeBatch``."""
    from tools.defi.trade_tool import DefiTradeTool, TransferParams
    s, r = rig, rig.r
    acc1 = r.account_of(1)
    human = "0x00000000000000000000000000000000000ba5e1"
    trade = DefiTradeTool(wallet=s.wallet, price_fn=lambda c, a: ETH_USD,
                          fallback_price_fn=lambda c, a: ETH_USD, account_rpc=r.core_rpc)
    weth = lambda a: r.uint(R.WETH_TESTNET, "balanceOf(address)", a)  # noqa: E731
    a0, h0 = r.balance(acc1), r.balance(human)
    res = _run(trade.transfer(TransferParams(chain=R.CHAIN, token="native", to=human, amount=0.0001,
                                             max_spend_usd=10.0, nft="1", dry_run=False)))
    print("\n  transfer native:", res.error or res.extracted_content)
    assert res.error is None and "SENT AND CONFIRMED" in res.extracted_content, res.error or res.extracted_content
    assert a0 - r.balance(acc1) == 10 ** 14 and r.balance(human) - h0 == 10 ** 14
    res = _run(trade.transfer(TransferParams(chain=R.CHAIN, token=R.WETH_TESTNET, to=human, amount=1.5,
                                             max_spend_usd=10.0, nft="1", dry_run=False)))
    print("  transfer WETH:", res.error or res.extracted_content)
    assert res.error is None and "SENT AND CONFIRMED" in res.extracted_content, res.error or res.extracted_content
    assert weth(acc1) == 35 * 10 ** 17 and weth(human) == 15 * 10 ** 17
    # the same act, the account named by its address (account=)
    res = _run(trade.transfer(TransferParams(chain=R.CHAIN, token="native", to=human, amount=0.0001,
                                             max_spend_usd=10.0, account=acc1, dry_run=False)))
    assert res.error is None and "SENT AND CONFIRMED" in res.extracted_content, res.error or res.extracted_content
    assert r.balance(human) - h0 == 2 * 10 ** 14
    booked = [e for e in s.gate.audit_log if e.get("account")]
    assert [e["action"] for e in booked][-3:] == ["transfer", "transfer", "transfer"]
    entries = r.rpc("eth_getLogs", [{"address": r.journal_log, "fromBlock": hex(r.deploy_block),
                                     "toBlock": "latest"}])
    from_acc = [e for e in entries if e["topics"][1][-40:] == acc1.lower()[2:]]
    print(f"  on-chain journal entries from the account: {len(from_acc)}")
    assert len(from_acc) >= 3
    # the account may not pay the pinned sale (a mint from the account is not the mint shape)
    res = _run(trade.transfer(TransferParams(chain=R.CHAIN, token="native", to=r.sale, amount=0.033,
                                             max_spend_usd=10.0, nft="1", dry_run=False)))
    text = res.error or res.extracted_content or ""
    print("  account pays the sale:", text[-300:])
    assert "pinned sale" in text and "SENT AND CONFIRMED" not in text, text


def _reveal_tool(s):
    from tools.agent_nft.tool import AgentNftTool
    return AgentNftTool(wallet=s.wallet, rpc_fn=s.r.core_rpc, price_fn=lambda c, a: ETH_USD)


def _reveal(s, max_ids=None, dry=False):
    """The verb; *max_ids* None = not set (the batch is sized to the fee cap)."""
    from tools.agent_nft.tool import RevealParams
    kw = {} if max_ids is None else {"max_ids": max_ids}
    res = _run(_reveal_tool(s).agent_nft_collection_reveal(
        RevealParams(chain=R.CHAIN, dry_run=dry, **kw)))
    assert res.error is None, res.error
    return res.extracted_content


def _due(r, max_ids=10_000):
    from tools.agent_nft import reveal as RV
    head = RV.read_head(r.core_rpc)
    due, _ntr, _nid = RV.due_ids(r.core_rpc, r.collection, head=head, max_ids=max_ids)
    return due


def _gas_used(out: str) -> int:
    import re
    m = re.search(r"gas used (\d+)", out)
    return int(m.group(1)) if m else 0


def _taken(out: str) -> int:
    import re
    return int(re.search(r"this run takes (\d+)", out).group(1))


def test_09_reveal_and_the_sell_out_burst(rig, monkeypatch):
    """The revealer (``agent_nft_collection_reveal``, the cron write job's verb) reveals the due
    ids through the guard; then a SELL-OUT BURST (30 public mints of 10 by another buyer, one per
    block) and the backlog it leaves. B8: a run takes as many due ids as the fee cap lets fit
    (sized from the guard's simulation, at most 64), oldest-due first, an explicit ``max_ids``
    still bounds it, and a run that leaves a backlog says how many remain and how many pass
    their window before the next run. Past 256 blocks an unrevealed id RE-COMMITS and the run
    says so (ALERT)."""
    import math
    import re
    from tools.agent_nft import reveal as RV
    s, r = rig, rig.r
    r.mine(3)
    print()
    runs = 0
    while _due(r):
        out = _reveal(s)
        runs += 1
        assert "confirmed" in out and "RE-COMMITTED" not in out, out
    print(f"  the treasury's {len(s.minted)} ids: revealed in {runs} run(s); nothing due")
    assert runs == 1, "a valueless testnet prices the fee at $0: one run takes every due id (≤ 64)"
    assert "nothing to reveal" in _reveal(s)
    assert all(r.uint(r.collection, "isRevealed(uint256)", i) for i in s.minted)

    buyer = "0x00000000000000000000000000000000000b0b00"
    r.fund(buyer, 1000 * 10 ** 18)
    burst_txs, qty = 30, 10
    first = r.next_id()
    gas = []
    for _ in range(burst_txs):
        rc = r.send(buyer, r.sale, r.encode("mint(address,uint256,uint256)", buyer, qty, 0),
                    value=qty * R.BASE)
        gas.append(int(rc["gasUsed"], 16))
    r.mine(3)
    minted = r.next_id() - first
    backlog = len(_due(r))
    auto = minted - backlog
    print(f"  burst: {burst_txs} mints x {qty} = {minted} ids in {burst_txs} blocks; each mint "
          f"auto-reveals up to 4 → {auto} auto-revealed, BACKLOG {backlog} due ids "
          f"(mint gas {min(gas)}..{max(gas)}, {max(gas) // qty} per id)")
    assert minted == burst_txs * qty and backlog > 10

    # an explicit max_ids still bounds the run, and the run says what it leaves
    before = len(_due(r))
    out10 = _reveal(s, max_ids=10)
    g10 = _gas_used(out10)
    assert "revealed 10 " in out10 and "10 (max_ids)" in out10, out10
    assert f"{before - 10} due id(s) remain" in out10, out10
    print("  max_ids 10:", next(l for l in out10.splitlines() if "backlog:" in l).strip())
    out_dry64 = _reveal(s, max_ids=64, dry=True)
    cost64 = re.search(r"cost:\s+(\S+)", out_dry64).group(1)
    out64 = _reveal(s, max_ids=64)
    g64 = _gas_used(out64)
    assert "revealed 64 " in out64, out64
    per_id = (g64 - g10) / 54

    # the SIZED run at 4663's fee market: base fee 0.024 gwei (the rail's maxFeePerGas = 2 × base
    # + its 0.1 gwei floor tip) and ETH at $4,000. The rig chain is valueless — the guard prices
    # its fee at $0 — so the sizing alone is given the 4663 price; the guard still decides.
    r.rpc("anvil_setNextBlockBaseFeePerGas", [hex(24_000_000)])
    r.mine(1)
    monkeypatch.setattr(RV, "native_usd", lambda chain, price_fn: ETH_USD)
    before = len(_due(r))
    out_sized = _reveal(s)
    n = _taken(out_sized)
    g_n = _gas_used(out_sized)
    fee = int(re.search(r"maxFeePerGas (\d+) wei", out_sized).group(1))
    worst = lambda g, f=fee: g * 1.5 * f * ETH_USD / 1e18  # noqa: E731
    sizing = next(l for l in out_sized.splitlines() if "sizing:" in l).strip()
    backlog = next(l for l in out_sized.splitlines() if "backlog:" in l).strip()
    print(f"  sized run (no max_ids): {sizing}\n  sized run: revealed {n}, gas {g_n}, the guard's "
          f"worst-case fee at 4663 prices ${worst(g_n):.3f} (cap $1.00)\n  sized run: {backlog}")
    assert f"revealed {n} " in out_sized and "(simulation)" in out_sized, out_sized
    assert 20 <= n <= 40 and n > 10 and worst(g_n) <= 1.0, (n, g_n, worst(g_n))
    assert worst(g_n + 2 * per_id) > 0.9, "the batch leaves no more than 2 ids of the cap unused"
    assert f"{before - n} due id(s) remain" in out_sized, out_sized
    monkeypatch.undo()
    left = len(_due(r))
    print(f"  reveal(10): gas {g10}; reveal(64): gas {g64}; ≈{per_id:.0f} gas per extra id; the "
          f"guard's worst-case fee at 4663 prices: reveal(10) ${worst(g10):.3f}, reveal(64) "
          f"${worst(g64):.3f}; this testnet run priced reveal(64) at {cost64}")
    public = 6551 - 131
    sellout_backlog = public - 4 * math.ceil(public / 10)
    print(f"  B8 projection: a sell-out of the {public} public ids at 10 per mint leaves "
          f"≈{sellout_backlog} ids for the revealer; at {n} ids per run (the $1.00 default cap) "
          f"that is {math.ceil(sellout_backlog / n)} runs, at 64 per run "
          f"{math.ceil(sellout_backlog / 64)} runs — the commit window is 256 L1 blocks (≈51 min, "
          f"≈51 one-minute runs = {51 * n} ids at {n}, {51 * 64} at 64)")

    # the backlog line warns when ids will pass their window before the next run
    assert left > 0
    head = RV.read_head(r.core_rpc)
    entries, _ntr, _nid = RV.due_entries(r.core_rpc, r.collection, head=head)
    oldest = min(rb for _i, rb in entries)
    r.mine(max(0, oldest + RV.REVEAL_WINDOW_BLOCKS - 3 - head))
    out_late = _reveal(s, max_ids=1, dry=True)
    late = [l.strip() for l in out_late.splitlines() if "backlog:" in l or "WARNING" in l]
    print("  near the window's end:", " | ".join(late))
    assert "WARNING" in out_late and "re-commit before the next run" in out_late, out_late

    # past the window: the next run RE-COMMITS what waited (and says so)
    r.mine(260)
    out = _reveal(s, max_ids=10)
    print("  after 260 blocks:", out.splitlines()[-1])
    assert "RE-COMMITTED" in out and "ALERT" in out, out


def test_10_fee_collect_through_the_account_needs_a_v4_position(rig):
    """115 W4 (``lp_collect`` v4 with ``account=``) needs the mint's v4 position. On this rig the
    pool leg is absent (46630 path: no PoolManager, no PNL, no pool), so every mint took the ETH
    fallback and ``positionOf(id)`` is 0: there is no position to collect from."""
    r = rig.r
    hook = r.address_of(r.collection, "POOLER()")
    assert r.code("0x8366a39CC670B4001A1121B8F6A443A643e40951") in ("0x", "0x0")
    assert r.uint(hook, "poolReady()") == 0
    pytest.skip("fee collect NOT PROVEN: no v4 PoolManager/PositionManager/PNL pool on the rig "
                "(the 46630 ETH-fallback path), so no mint opened a position (poolReady() false; "
                "the desk proves it only on a 4663 fork through its relay, e2e_local.sh step 4)")


def _future_account(r, token_id):
    from core.wallet import erc6551
    return erc6551.account_address(R.CHAIN_ID, r.collection, token_id, implementation=r.account_impl)


def test_11_send_the_nft_out_and_the_watch_reports_it(rig):
    """``agent_nft_withdraw_token`` (``/nft send``'s verb) sends #2 to a plain wallet through the
    guard (``safeTransferFrom``, rule 5: no open approval); the holdings watch reports it left,
    once; the account of #2 can no longer be acted through."""
    from core.wallet import nft_holdings
    from tools.agent_nft.tool import AgentNftTool, TakeParams
    from tools.defi.trade_tool import DefiTradeTool, TransferParams
    s, r = rig, rig.r
    human = "0x00000000000000000000000000000000000ba5e2"
    tool = AgentNftTool(wallet=s.wallet, rpc_fn=r.core_rpc)
    # nesting: never into the PolyrobAccount of an id of the collection, minted or not yet
    for nest in (r.account_of(3), _future_account(r, 6000)):
        res = _run(tool.agent_nft_withdraw_token(TakeParams(chain=R.CHAIN, to=nest, nft="2",
                                                            dry_run=False)))
        text = res.error or res.extracted_content or ""
        print(f"\n  withdraw into {nest}: {text[-300:]}")
        assert "confirmed" not in text and r.owner_of(2) == s.signer.address.lower(), text
    res = _run(tool.agent_nft_withdraw_token(TakeParams(chain=R.CHAIN, to=human, nft="2",
                                                        dry_run=False)))
    print("\n  withdraw:", res.error or res.extracted_content)
    assert res.error is None and "confirmed" in res.extracted_content, res.error or res.extracted_content
    assert r.owner_of(2) == human

    async def notify(_c, _u, text):
        s.told.append(text)
    n = len(s.told)
    res = _run(nft_holdings.tick(None, treasury=s.signer.address, rpc_for=lambda c: r.core_rpc,
                                 notify=notify))
    assert [x["token_id"] for x in res.lost] == [2], res.as_dict()
    assert len(s.told) == n + 1 and "is no longer mine" in s.told[-1], s.told[-1]
    print("  watch:", s.told[-1])
    again = _run(nft_holdings.tick(None, treasury=s.signer.address, rpc_for=lambda c: r.core_rpc,
                                   notify=notify))
    assert again.lost == [] and len(s.told) == n + 1
    trade = DefiTradeTool(wallet=s.wallet, price_fn=lambda c, a: ETH_USD,
                          fallback_price_fn=lambda c, a: ETH_USD, account_rpc=r.core_rpc)
    res = _run(trade.transfer(TransferParams(chain=R.CHAIN, token="native", to=human, amount=0.0001,
                                             max_spend_usd=10.0, nft="2", dry_run=False)))
    print("  act through #2 after it left:", res.error)
    assert res.error and "does not own" in res.error, res.error or res.extracted_content


# ---------------------------------------------------------------------------------- 115 W7: sell

FILLER = "0x00000000000000000000000000000000000f1110"


def _nft_tool(s):
    from tools.agent_nft.tool import AgentNftTool
    return AgentNftTool(wallet=s.wallet, rpc_fn=s.r.core_rpc, price_fn=lambda c, a: ETH_USD)


def _tx_of(out: str) -> str:
    import re
    return re.search(r"tx:\s+(0x[0-9a-f]{64})", out).group(1)


def _ready_of(r, token_id: int) -> bool:
    return bool(r.uint(r.collection, "saleReadyOf(uint256)", token_id))


def _plain_fill_error(s, token_id: int):
    """A plain marketplace fill, staged: the treasury approved FILLER for the token; FILLER calls
    ``transferFrom(treasury, FILLER, id)``. None when it would pass, else the revert text."""
    r = s.r
    try:
        r.call(r.collection, r.encode("transferFrom(address,address,uint256)", s.signer.address,
                                      FILLER, token_id), frm=FILLER)
        return None
    except RuntimeError as exc:
        return str(exc)


def _watch(s):
    from core.wallet import nft_holdings

    async def notify(_c, _u, text):
        s.told.append(text)
    return _run(nft_holdings.tick(None, treasury=s.signer.address, rpc_for=lambda c: s.r.core_rpc,
                                  notify=notify))


def test_12_ready_with_the_journal_in_one_transaction(rig):
    """``agent_nft_sale_ready`` on #1 through the real guard: ONE ``executeBatch([readyToSell(1),
    JournalLog.log(entry)])`` by the treasury; the receipt holds one ``SaleReady(1, treasury, …)``
    from the collection and the journal ``Entry`` from the account; nothing of the account moved;
    ``saleReadyOf(1)`` is true; a staged plain fill (an approved operator's ``transferFrom``)
    would now pass; the watch says READY FOR SALE once."""
    from core.wallet import sale_acts as A
    from tools.agent_nft.tool import SaleReadyParams
    s, r = rig, rig.r
    acc1 = r.account_of(1)
    weth = lambda a: r.uint(R.WETH_TESTNET, "balanceOf(address)", a)  # noqa: E731
    before = (r.balance(acc1), weth(acc1))
    _watch(s)
    r.fund(FILLER)
    r.send(s.signer.address, r.collection, r.encode("approve(address,uint256)", FILLER, 1))
    assert _plain_fill_error(s, 1) and "620e721c" in _plain_fill_error(s, 1)   # not ready yet
    dry = _run(_nft_tool(s).agent_nft_sale_ready(SaleReadyParams(chain=R.CHAIN, nft="1")))
    print("\n  ready (dry):", dry.error or dry.extracted_content)
    assert dry.error is None and "DRY RUN" in dry.extracted_content
    res = _run(_nft_tool(s).agent_nft_sale_ready(SaleReadyParams(chain=R.CHAIN, nft="1",
                                                                 dry_run=False)))
    out = res.error or res.extracted_content
    print("  ready:", out)
    assert res.error is None and "(confirmed)" in out, out
    rc = r.receipt(_tx_of(out))
    ready_logs = [lg for lg in rc["logs"] if lg["topics"][0] == A.TOPIC_SALE_READY]
    assert len(ready_logs) == 1 and ready_logs[0]["address"].lower() == r.collection
    assert int(ready_logs[0]["topics"][1], 16) == 1
    assert ready_logs[0]["topics"][2][-40:] == s.signer.address.lower()[2:]
    entries = [lg for lg in rc["logs"] if lg["address"].lower() == r.journal_log]
    assert len(entries) == 1 and entries[0]["topics"][1][-40:] == acc1.lower()[2:]
    assert rc["to"].lower() == acc1.lower()
    assert (r.balance(acc1), weth(acc1)) == before
    assert _ready_of(r, 1) is True
    assert _plain_fill_error(s, 1) is None, "a plain fill must pass while the NFT is ready"
    _watch(s)
    assert "READY FOR SALE" in s.told[-1] and "#1" in s.told[-1], s.told[-1]


def test_13_an_automatic_act_through_the_ready_account_is_held(rig):
    """The sale hold: ``defi_trade.transfer`` from #1's account with no owner turn (an automatic
    act) is refused BEFORE the simulation; readiness stays."""
    from core.wallet import sale_acts as A
    from tools.defi.trade_tool import DefiTradeTool, TransferParams
    s, r = rig, rig.r
    trade = DefiTradeTool(wallet=s.wallet, price_fn=lambda c, a: ETH_USD,
                          fallback_price_fn=lambda c, a: ETH_USD, account_rpc=r.core_rpc)
    res = _run(trade.transfer(TransferParams(chain=R.CHAIN, token="native", to=FILLER,
                                             amount=0.0001, max_spend_usd=10.0, nft="1",
                                             dry_run=False)))
    text = res.error or res.extracted_content or ""
    print("\n  automatic act:", text[-300:])
    assert A.HOLD_TEXT in text and "SENT AND CONFIRMED" not in text, text
    assert _ready_of(r, 1) is True and _plain_fill_error(s, 1) is None


def test_14_the_owners_act_passes_and_ends_readiness(rig, monkeypatch):
    """An act the OWNER asked for (a genuine owner turn: the bound owner principal, no forged
    turn) passes the hold with the note that readiness ends; after it ``saleReadyOf(1)`` is false
    and the same staged plain fill reverts ``SaleNotReady``; the watch says readiness lost."""
    import types
    from core.wallet import erc6551, sale_acts as A, tx_guard
    from core.wallet.broadcast.evm import EvmRail
    s, r = rig, rig.r
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rig-owner")
    ctx = types.SimpleNamespace(user_id="rig-owner", role="orchestrator", is_sub_agent=False,
                                metadata={}, session_id="rig")
    acc1 = r.account_of(1)
    rail = EvmRail(chain=R.CHAIN, signer=s.signer)
    state = erc6551.read_state(r.core_rpc, acc1)
    tx = rail.build_call(to=acc1, value=0, data=erc6551.encode_execute(FILLER, 10 ** 14, "0x", 0))
    intent = tx_guard.TxIntent(chain=R.CHAIN, token=None, to=FILLER, amount_raw=10 ** 14,
                               max_spend_usd=10.0, via_account=acc1, via_account_state=state,
                               idempotency_key=f"rig-owner-act-{state}", is_transfer=True)
    d = tx_guard.authorize(intent, tx, holder=s.signer.address, gate=s.gate,
                           execution_context=ctx, price_fn=lambda c, a: ETH_USD,
                           forged_fn=lambda c, t: False, account_rpc=r.core_rpc)
    print(f"\n  owner act: {d.reason} (lane {d.lane})")
    assert d.allowed and d.lane == "owner_direct" and f"NOTE: {A.HOLD_NOTE}" in d.reason, d.reason
    f0 = r.balance(FILLER)
    _send(s, intent, tx, rail, d, action="transfer")
    assert r.balance(FILLER) - f0 == 10 ** 14
    assert _ready_of(r, 1) is False
    err = _plain_fill_error(s, 1)
    print("  plain fill after the act:", err)
    assert err and "620e721c" in err, err   # SaleNotReady()
    monkeypatch.delenv("POLYROB_OWNER_USER_ID")   # the watch's state lives under the owner tenant
    _watch(s)
    assert "readiness lost, no transfer" in s.told[-1] and "#1" in s.told[-1], s.told[-1]


def test_15_ready_again_then_unready(rig):
    """Ready again (a new state, so a new replay key), then ``agent_nft_sale_unready``:
    ``cancelReady(1)`` from the treasury moves nothing and readiness is false after; the staged
    fill reverts again. Unready when not ready sends nothing."""
    from tools.agent_nft.tool import SaleReadyParams
    s, r = rig, rig.r
    tool = _nft_tool(s)
    res = _run(tool.agent_nft_sale_ready(SaleReadyParams(chain=R.CHAIN, nft="1", dry_run=False)))
    assert res.error is None and "(confirmed)" in res.extracted_content, res.error or res.extracted_content
    assert _ready_of(r, 1) is True and _plain_fill_error(s, 1) is None
    acc1 = r.account_of(1)
    state = r.uint(acc1, "state()")
    t0 = r.balance(s.signer.address)
    res = _run(tool.agent_nft_sale_unready(SaleReadyParams(chain=R.CHAIN, nft="1", dry_run=False)))
    out = res.error or res.extracted_content
    print("\n  unready:", out)
    assert res.error is None and "(confirmed)" in out, out
    rc = r.receipt(_tx_of(out))
    assert rc["to"].lower() == r.collection and rc["from"].lower() == s.signer.address.lower()
    assert _ready_of(r, 1) is False and "620e721c" in (_plain_fill_error(s, 1) or "")
    assert r.uint(acc1, "state()") == state, "cancelReady from the treasury leaves the account's state"
    assert t0 - r.balance(s.signer.address) < 10 ** 15   # the fee only
    again = _run(tool.agent_nft_sale_unready(SaleReadyParams(chain=R.CHAIN, nft="1")))
    assert "not ready — no transaction" in again.extracted_content
    r.send(s.signer.address, r.collection, r.encode("approve(address,uint256)",
                                                    "0x" + "00" * 20, 1))


def test_16_cancel_market_orders(rig):
    """``agent_nft_sale_cancel_listing`` on the PINNED market the rig deployed (``Deploy.s.sol``):
    without a salt ``incrementNonce()`` (``NonceIncremented(treasury, 1)``, nonce 0 → 1); with a
    salt ``cancel(7)`` (``usedSalt(treasury, 7)`` true). A used salt sends nothing."""
    from core.wallet import sale_acts as A
    from tools.agent_nft.tool import SaleCancelListingParams
    s, r = rig, rig.r
    tool = _nft_tool(s)
    me = s.signer.address
    assert r.uint(r.market, "nonces(address)", me) == 0
    res = _run(tool.agent_nft_sale_cancel_listing(SaleCancelListingParams(chain=R.CHAIN,
                                                                          dry_run=False)))
    out = res.error or res.extracted_content
    print("\n  cancel all:", out)
    assert res.error is None and "(confirmed)" in out, out
    rc = r.receipt(_tx_of(out))
    assert [lg["topics"][0] for lg in rc["logs"]] == [A.TOPIC_NONCE_INCREMENTED]
    assert r.uint(r.market, "nonces(address)", me) == 1
    res = _run(tool.agent_nft_sale_cancel_listing(SaleCancelListingParams(chain=R.CHAIN, salt=7,
                                                                          dry_run=False)))
    out = res.error or res.extracted_content
    print("  cancel salt 7:", out)
    assert res.error is None and "(confirmed)" in out, out
    assert r.uint(r.market, "usedSalt(address,uint256)", me, 7) == 1
    again = _run(tool.agent_nft_sale_cancel_listing(SaleCancelListingParams(chain=R.CHAIN, salt=7)))
    assert "already used or cancelled" in again.extracted_content


def test_17_an_nft_that_leaves_while_ready_is_reported_sold_or_moved(rig):
    """#3 is made ready, then a plain fill takes it (an approved operator's ``transferFrom``, the
    rig's stand-in for a marketplace): the watch reports it SOLD OR MOVED while ready, with the
    transaction, and says it does not know the price (no Sold event of the pinned market)."""
    from tools.agent_nft.tool import SaleReadyParams
    s, r = rig, rig.r
    res = _run(_nft_tool(s).agent_nft_sale_ready(SaleReadyParams(chain=R.CHAIN, nft="3",
                                                                 dry_run=False)))
    assert res.error is None and "(confirmed)" in res.extracted_content, res.error or res.extracted_content
    _watch(s)
    r.send(s.signer.address, r.collection, r.encode("approve(address,uint256)", FILLER, 3))
    rc = r.send(FILLER, r.collection, r.encode("transferFrom(address,address,uint256)",
                                               s.signer.address, FILLER, 3))
    assert r.owner_of(3) == FILLER.lower()
    res = _watch(s)
    assert [x["token_id"] for x in res.lost] == [3], res.as_dict()
    text = s.told[-1]
    print("\n  watch:", text)
    assert "SOLD OR MOVED while it was ready for sale" in text and rc["transactionHash"] in text
    assert "do not know the price" in text


# ---------------------------------------------------------------------------------- 115 W6: card

def test_18_publish_the_card_through_the_account(rig):
    """``agent_nft_bind_identity`` on #1 (an ERC-6551P token whose account owns the identity its
    mint registered): the holdings watch records the agentId; then ONE ``account.execute(
    identityRegistry, setAgentURI(agentId, card))`` through the real guard, measured by the
    registry's ``URIUpdated(agentId, card, account)``; the registry's ``tokenURI`` reads the card
    back, the view says published, and a second call sends nothing. The registry is the rig's
    ``IdentityCardStandIn`` (the desk mock has no ``setAgentURI``), etched over the mock for this
    test only and seeded with the mock's owner; the mock is restored after."""
    import base64
    import json
    from core.wallet import collection_registry as CR
    from core.wallet import erc8004_reputation as REP
    from core.wallet import identity_card as C
    from core.wallet import nft_holdings
    from tools.agent_nft import view as V
    from tools.agent_nft.tool import BindParams
    from eth_account import Account
    s, r = rig, rig.r
    acc1 = r.account_of(1)
    agent_id = REP.agent_id_of(r.core_rpc, r.collection, 1)
    assert r.address_of(R.IDENTITY_TESTNET, "ownerOf(uint256)", agent_id).lower() == acc1.lower()
    _watch(s)
    (row,) = [x for x in nft_holdings.tracked(chain=R.CHAIN)
              if x["token_id"] == 1 and x["collection"] == r.collection]
    assert row["agent_id"] == agent_id and row["identity_held"] is True, row
    mock = r.code(R.IDENTITY_TESTNET)
    r.rpc("anvil_setCode", [R.IDENTITY_TESTNET, r.card_standin])
    try:
        helper = Account.create().address
        r.fund(helper)
        r.send(helper, R.IDENTITY_TESTNET, r.encode("seed(uint256,address,string)", agent_id, acc1,
                                                    "data:,minted-card"))
        tool = _nft_tool(s)
        dry = _run(tool.agent_nft_bind_identity(BindParams(chain=R.CHAIN, nft="1")))
        print("\n  publish (dry):", dry.error or dry.extracted_content)
        assert dry.error is None and "DRY RUN" in dry.extracted_content
        assert f"setAgentURI(agentId {agent_id}) THROUGH the account" in dry.extracted_content
        assert "this is a call THROUGH the account" in dry.extracted_content
        res = _run(tool.agent_nft_bind_identity(BindParams(chain=R.CHAIN, nft="1", dry_run=False)))
        out = res.error or res.extracted_content
        print("  publish:", out)
        assert res.error is None and "(confirmed)" in out, out
        rc = r.receipt(_tx_of(out))
        assert rc["to"].lower() == acc1.lower()
        updated = [lg for lg in rc["logs"] if lg["topics"][0] == C.TOPIC_URI_UPDATED]
        assert len(updated) == 1 and updated[0]["address"].lower() == R.IDENTITY_TESTNET.lower()
        assert int(updated[0]["topics"][1], 16) == agent_id
        assert updated[0]["topics"][2][-40:] == acc1.lower()[2:]
        card = C.read_token_uri(r.core_rpc, R.CHAIN, agent_id)
        doc = json.loads(base64.b64decode(card.split(",", 1)[1]))
        assert doc["registrations"] == [{"agentId": agent_id,
                                         "agentRegistry": f"eip155:{R.CHAIN_ID}:{R.IDENTITY_TESTNET}"}]
        assert [x["endpoint"].lower() for x in doc["services"] if x["name"] == "agentWallet"] == \
            [f"eip155:{R.CHAIN_ID}:{acc1}".lower()]
        (profile,) = CR.load()
        v = V.build_view(r.core_rpc, R.CHAIN, profile, 1, treasury=s.signer.address)
        print("  view card:", v.card)
        assert v.card is True and "card:      published: yes" in V.render(v)
        again = _run(tool.agent_nft_bind_identity(BindParams(chain=R.CHAIN, nft="1", dry_run=False)))
        assert "already publishes this card: no transaction" in again.extracted_content
    finally:
        r.rpc("anvil_setCode", [R.IDENTITY_TESTNET, mock])
    assert r.address_of(R.IDENTITY_TESTNET, "ownerOf(uint256)", agent_id).lower() == acc1.lower()


# ---------------------------------------------------------------------------------- 115 W9: buy
# A SECOND fresh key mints and holds an NFT and signs a market Listing (EIP-712, here in the test
# with eth_account — no core signing path); the treasury buys it through the real guard.

def _seller_with_listing(s, *, eth_in_account=3 * 10 ** 15, weth_in_account=2 * 10 ** 18,
                         price=5 * 10 ** 15):
    """A fresh seller key mints one id through the sale, funds its account, approves the market,
    and signs a Listing exactly as the market verifies it. Returns ``(seller, id, link)``."""
    import secrets
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from core.wallet import collection_mint as M, sale_conditions as S
    r = s.r
    seller = Account.create()
    r.fund(seller.address, 10 ** 19)
    tid = r.next_id()
    r.send(seller.address, r.sale, M.encode_mint(seller.address, 1, 0), value=r.price_now())
    assert r.owner_of(tid) == seller.address.lower()
    acct = r.account_of(tid)
    r.fund(acct, eth_in_account)
    helper = Account.create().address
    r.fund(helper)
    r.send(helper, R.WETH_TESTNET, r.encode("mint(address,uint256)", acct, weth_in_account))
    r.send(seller.address, r.collection, r.encode("setApprovalForAll(address,bool)", r.market, True))
    dom = r.call(r.market, S.EIP712_DOMAIN_SELECTOR)
    import core.wallet.abi as abi
    _f, name, version, cid, verifying, _salt, _ext = abi.decode(
        [{"type": "bytes1"}, {"type": "string"}, {"type": "string"}, {"type": "uint256"},
         {"type": "address"}, {"type": "bytes32"}, {"type": "uint256[]"}], dom)
    now = int(r.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
    listing = {"collection": r.collection, "id": tid,
               "transferCount": r.uint(r.collection, "transferCount(uint256)", tid),
               "seller": seller.address.lower(), "currency": "0x" + "00" * 20, "price": price,
               "maxRoyalty": 0, "expiry": now + 7 * 86_400, "salt": secrets.randbits(128),
               "promiseHash": S.conditions_hash([]),
               "nonce": r.uint(r.market, "nonces(address)", seller.address)}
    fields = [("collection", "address"), ("id", "uint256"), ("transferCount", "uint256"),
              ("seller", "address"), ("currency", "address"), ("price", "uint256"),
              ("maxRoyalty", "uint256"), ("expiry", "uint256"), ("salt", "uint256"),
              ("promiseHash", "bytes32"), ("nonce", "uint256")]
    typed = {"types": {"EIP712Domain": [{"name": "name", "type": "string"},
                                        {"name": "version", "type": "string"},
                                        {"name": "chainId", "type": "uint256"},
                                        {"name": "verifyingContract", "type": "address"}],
                       "Listing": [{"name": n, "type": t} for n, t in fields]},
             "primaryType": "Listing",
             "domain": {"name": name, "version": version, "chainId": int(cid),
                        "verifyingContract": verifying},
             "message": dict(listing)}
    signed = Account.sign_message(encode_typed_data(full_message=typed), seller.key)
    on_chain = r.call(r.market, S.encode_listing_digest(S.Listing.of(listing)))
    assert on_chain == "0x" + bytes(signed.message_hash).hex(), "the market verifies another digest"
    payload = {"v": 1, "k": "m", "chainId": R.CHAIN_ID, "market": r.market,
               "collection": r.collection, "listing": listing,
               "sig": "0x" + bytes(signed.signature).hex(), "promise": []}
    return seller, tid, "http://127.0.0.1/buy.html?id=%d#x=%s" % (tid, S.encode_link(payload))


def _quote(s, link):
    import re
    from tools.agent_nft.tool import BuyParams
    dry = _run(_nft_tool(s).agent_nft_buy(BuyParams(chain=R.CHAIN, listing=link,
                                                    max_spend_usd=50.0)))
    out = dry.error or dry.extracted_content
    assert dry.error is None and "DRY RUN" in out, out
    return out, re.search(r"conditions_hash (0x[0-9a-f]{64})", out).group(1)


def test_30_buy_a_market_listing_through_the_guard(rig):
    """A second fresh key's #id is bought through ``agent_nft_buy``: the quote names what the
    account holds (ETH, the WETH, the identity) and the conditions; the buy pays EXACTLY the price
    to the seller, the one NFT arrives in the treasury, the market emits ONE ``Sold`` and holds
    nothing after; the watch reports the arrival; the bought account is not ready (the hold
    needs no exception: a transfer ends readiness)."""
    from core.wallet import sale_conditions as S
    from tools.agent_nft.tool import BuyParams
    s, r = rig, rig.r
    seller, tid, link = _seller_with_listing(s)
    _watch(s)
    quote, chash = _quote(s, link)
    print("\n  buy (dry):", quote)
    assert "WETH" in quote and "Agent identity" in quote and "CHECKED" in quote
    seller0, treasury0, market0 = (r.balance(seller.address), r.balance(s.signer.address),
                                   r.balance(r.market))
    res = _run(_nft_tool(s).agent_nft_buy(BuyParams(chain=R.CHAIN, listing=link,
                                                    max_spend_usd=50.0, dry_run=False,
                                                    conditions_hash=chash)))
    out = res.error or res.extracted_content
    print("  buy:", out)
    assert res.error is None and "(confirmed)" in out, out
    rc = r.receipt(_tx_of(out))
    assert rc["to"].lower() == r.market and rc["from"].lower() == s.signer.address.lower()
    sold = [lg for lg in rc["logs"] if lg["topics"][0] == S.TOPIC_SOLD]
    assert len(sold) == 1 and sold[0]["address"].lower() == r.market
    assert int(sold[0]["topics"][2], 16) == tid
    assert r.owner_of(tid) == s.signer.address.lower()
    price = 5 * 10 ** 15
    assert r.balance(seller.address) - seller0 == price
    paid = treasury0 - r.balance(s.signer.address)
    assert price <= paid < price + 10 ** 15, paid
    assert r.balance(r.market) == market0
    assert _ready_of(r, tid) is False
    res = _watch(s)
    assert tid in [x["token_id"] for x in res.arrived], res.as_dict()
    print("  watch:", s.told[-1])


def test_31_a_seller_who_empties_the_account_after_the_quote_is_paid_nothing(rig):
    """The seller takes the account's ETH after the quote. (1) The buy of that quote refuses: the
    account changed since. (2) The guard refuses the quoted transaction before simulating it (a
    fresh read builds another list). (3) The quoted transaction, simulated with the guard's
    re-read skipped, is refused by the SIMULATION, named: the market reverts ConditionFailed.
    (4) Sent raw, it reverts on chain and pays nothing. The NFT stays with the seller."""
    import dataclasses
    from core.wallet import erc6551, sale_conditions as S, tx_guard, tx_guard_sale
    from core.wallet.broadcast.evm import EvmRail
    from tools.agent_nft import sale_buy
    from tools.agent_nft.tool import BuyParams
    s, r = rig, rig.r
    seller, tid, link = _seller_with_listing(s)
    _quoted, chash = _quote(s, link)
    params = BuyParams(chain=R.CHAIN, listing=link, max_spend_usd=50.0)
    profile, listing, data, _h, _snap, _built = sale_buy.quote(_nft_tool(s), params,
                                                               s.signer.address)
    acct = r.account_of(tid)
    took = r.balance(acct)
    r.send(seller.address, acct, erc6551.encode_execute(seller.address, took, "0x", 0))
    assert r.balance(acct) == 0
    t0 = r.balance(s.signer.address)
    res = _run(_nft_tool(s).agent_nft_buy(BuyParams(chain=R.CHAIN, listing=link,
                                                    max_spend_usd=50.0, dry_run=False,
                                                    conditions_hash=chash)))
    print("\n  buy after the seller emptied the account:", res.error)
    assert res.error and "changed since the quote" in res.error, res.error or res.extracted_content
    rail = EvmRail(chain=R.CHAIN, signer=s.signer)
    tx = rail.build_call(to=r.market, data=data, value=listing.price)
    intent = tx_guard.TxIntent(chain=R.CHAIN, token=None, to=r.market, amount_raw=listing.price,
                               max_spend_usd=50.0, idempotency_key=f"rig-stale-buy-{tid}",
                               expected_nft_in=((r.collection, "erc721", tid, 1),))
    d = _authorize(s, intent, tx)
    print("  guard on the quoted tx:", d.reason)
    assert not d.allowed and "fresh read" in d.reason, d.reason
    real = tx_guard_sale.runtime_refusal
    tx_guard_sale.runtime_refusal = lambda *a, **k: None     # the read raced the simulation
    try:
        d = _authorize(s, dataclasses.replace(intent, idempotency_key=f"rig-stale-buy2-{tid}"), tx)
    finally:
        tx_guard_sale.runtime_refusal = real
    print("  guard, simulation:", d.reason)
    assert not d.allowed and "ConditionFailed" in d.reason, d.reason
    rc = r.send(s.signer.address, r.market, data, value=listing.price, expect_ok=False)
    assert int(rc["status"], 16) == 0
    assert r.owner_of(tid) == seller.address.lower()
    assert t0 - r.balance(s.signer.address) < listing.price, "only gas was spent"


def test_32_claim_what_the_market_owes(rig):
    """``agent_nft_market_claim``: an owed balance is STAGED (the treasury, an EOA, never refuses
    a payment, so the rig writes ``owed[treasury]`` and the market's ETH with anvil); the claim
    through the guard is measured (owed → 0, exactly that arrives); nothing owed = no transaction."""
    from eth_utils import keccak
    from core.wallet import sale_conditions as S
    from tools.agent_nft.tool import MarketClaimParams
    s, r = rig, rig.r
    me = s.signer.address
    owed = 7 * 10 ** 14
    for slot in range(12):
        key = "0x" + keccak(bytes.fromhex("00" * 12 + me.lower()[2:] + format(slot, "064x"))).hex()
        r.rpc("anvil_setStorageAt", [r.market, key, "0x" + format(owed, "064x")])
        if int(r.call(r.market, S.encode_owed(me)), 16) == owed:
            break
        r.rpc("anvil_setStorageAt", [r.market, key, "0x" + "00" * 32])
    else:
        pytest.fail("the market's owed mapping slot was not found")
    r.fund(r.market, r.balance(r.market) + owed)
    t0 = r.balance(me)
    res = _run(_nft_tool(s).agent_nft_market_claim(MarketClaimParams(chain=R.CHAIN,
                                                                     dry_run=False)))
    out = res.error or res.extracted_content
    print("\n  market claim:", out)
    assert res.error is None and "(confirmed)" in out, out
    assert int(r.call(r.market, S.encode_owed(me)), 16) == 0
    assert r.balance(me) - t0 > owed - 10 ** 14
    again = _run(_nft_tool(s).agent_nft_market_claim(MarketClaimParams(chain=R.CHAIN)))
    assert "owes this treasury nothing" in again.extracted_content


# ------------------------------------------------------------------------- 111 W9a: seller orders
# The treasury LISTS through the real path: the single-token approval of the market through the
# guard, then ONE class-O signature by the rig's LocalEoaSigner through sign_market_order (core
# reads every fact itself). A SECOND fresh key buys with the share payload by calling market.buy
# itself. A second key signs an Offer (test code); the treasury ACCEPTS through the guard. The
# owner gate (an owner turn or a granted card) is unit-tested; here it is stubbed open.

def _owner_open(s):
    from tools.agent_nft import sale_list
    s.mp.setattr(sale_list, "_owner_asked", lambda *a, **k: True)


def _fresh_treasury_nft(s):
    _guarded_mint(s, value=s.r.price_now())
    tid = s.minted[-1]
    assert s.r.owner_of(tid) == s.signer.address.lower()
    _watch(s)
    return tid


def test_33_list_through_class_o_and_a_second_key_buys_it(rig):
    import re
    from eth_account import Account
    from core.wallet import market_orders, sale_acts as A, sale_conditions as S, sale_orders as O
    from tools.agent_nft.tool import SaleListParams
    s, r = rig, rig.r
    _owner_open(s)
    tid = _fresh_treasury_nft(s)
    acct = r.account_of(tid)
    r.fund(acct, 2 * 10 ** 15)
    dry = _run(_nft_tool(s).agent_nft_sale_list(SaleListParams(chain=R.CHAIN, nft=str(tid),
                                                               price="0.004")))
    print("\n  list (dry):", dry.error or dry.extracted_content)
    assert dry.error is None and "DRY RUN" in dry.extracted_content
    assert "is NOT yet" in dry.extracted_content and "class O" in dry.extracted_content
    res = _run(_nft_tool(s).agent_nft_sale_list(SaleListParams(chain=R.CHAIN, nft=str(tid),
                                                               price="0.004", dry_run=False)))
    out = res.error or res.extracted_content
    print("  list:", out)
    assert res.error is None and "LISTED" in out and "(confirmed)" in out, out
    assert A.read_approved(r.core_rpc, r.collection, tid) == r.market
    assert not r.uint(r.collection, "isApprovedForAll(address,address)", s.signer.address, r.market)
    payload = S.decode_link(re.search(r"(#x=[A-Za-z0-9_-]+)", out).group(1))
    assert payload["k"] == "m" and payload["market"] == r.market and payload["chainId"] == R.CHAIN_ID
    listing = S.Listing.of(payload["listing"])
    promise = [S.Condition.of(c) for c in payload["promise"]]
    assert listing.seller == s.signer.address.lower() and listing.price == 4 * 10 ** 15
    # the market's own digest, signed by the treasury
    digest = r.call(r.market, S.encode_listing_digest(listing))
    assert O.recovers(digest, payload["sig"], s.signer.address)
    rows = market_orders.open_orders(at=market_orders.path())
    assert [int(x["salt"]) for x in rows if int(x["token_id"]) == tid] == [listing.salt]
    # a SECOND fresh key buys it with the payload, calling the market itself
    buyer = Account.create()
    r.fund(buyer.address, 10 ** 18)
    ctx = S.read_context(R.CHAIN)
    _snap, built = S.read_and_build(r.core_rpc, collection=r.collection, token_id=tid,
                                    account=acct, deploy_block=r.deploy_block, ctx=ctx)
    data = S.encode_buy(listing, payload["sig"], promise, built.conditions, buyer.address)
    t0 = r.balance(s.signer.address)
    rc = r.send(buyer.address, r.market, data, value=listing.price)
    assert int(rc["status"], 16) == 1
    assert r.owner_of(tid) == buyer.address.lower()
    got = r.balance(s.signer.address) - t0
    sold = [lg for lg in rc["logs"] if lg["topics"][0] == S.TOPIC_SOLD]
    assert len(sold) == 1
    royalty = int(sold[0]["data"][2 + 3 * 64:2 + 4 * 64], 16)
    assert got == listing.price - royalty and royalty <= listing.maxRoyalty
    assert A.read_approved(r.core_rpc, r.collection, tid) == S.ZERO    # the transfer cleared it
    res = _watch(s)
    assert tid in [x["token_id"] for x in res.lost], res.as_dict()
    told = [t for t in s.told if f"#{tid}" in t and "SOLD on its pinned market" in t]
    assert told and str(listing.price) in told[-1], s.told[-3:]
    print("  watch:", told[-1])
    assert not [x for x in market_orders.open_orders(at=market_orders.path())
                if int(x["token_id"]) == tid]


def _buyer_offer(s, tid, *, price=3 * 10 ** 15, expiry_in=86_400):
    """A fresh buyer key mints WETH, approves the market for the price and signs an Offer for
    #tid with the conditions core builds from a fresh read (test code, eth_account)."""
    import secrets
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from core.wallet import sale_conditions as S, sale_orders as O
    r = s.r
    buyer = Account.create()
    r.fund(buyer.address, 10 ** 18)
    r.send(buyer.address, R.WETH_TESTNET, r.encode("mint(address,uint256)", buyer.address, price))
    r.send(buyer.address, R.WETH_TESTNET, r.encode("approve(address,uint256)", r.market, price))
    _snap, built = S.read_and_build(r.core_rpc, collection=r.collection, token_id=tid,
                                    account=r.account_of(tid), deploy_block=r.deploy_block,
                                    ctx=S.read_context(R.CHAIN))
    now = int(r.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
    offer = O.Offer.of({"collection": r.collection, "id": tid, "anyId": False,
                        "buyer": buyer.address.lower(), "currency": R.WETH_TESTNET.lower(),
                        "price": price, "maxRoyalty": price // 20, "expiry": now + expiry_in,
                        "salt": secrets.randbits(128),
                        "conditionsHash": S.conditions_hash(built.conditions),
                        "nonce": r.uint(r.market, "nonces(address)", buyer.address)})
    name, version, cid, market = O.read_domain(r.core_rpc, r.market)
    fields = [{"name": n, "type": t} for n, t in zip(O._OFFER_FIELDS, O._OFFER_TYPES)]
    typed = {"types": {"EIP712Domain": O._DOMAIN_FIELDS, "Offer": fields}, "primaryType": "Offer",
             "domain": {"name": name, "version": version, "chainId": cid,
                        "verifyingContract": market},
             "message": dict(zip(O._OFFER_FIELDS, offer.as_tuple()))}
    signed = Account.sign_message(encode_typed_data(full_message=typed), buyer.key)
    assert r.call(r.market, O.encode_offer_digest(offer)) == "0x" + bytes(signed.message_hash).hex()
    payload = {"v": 1, "k": "o", "chainId": R.CHAIN_ID, "market": r.market,
               "collection": r.collection,
               "offer": {k: (str(v) if isinstance(v, int) and not isinstance(v, bool) else v)
                         for k, v in zip(O._OFFER_FIELDS, offer.as_tuple())},
               "sig": "0x" + bytes(signed.signature).hex(),
               "conditions": [c.as_json() for c in built.conditions]}
    return buyer, offer, "http://127.0.0.1/account.html#x=" + S.encode_link(payload)


def test_34_a_second_key_offers_and_the_treasury_accepts_through_the_guard(rig):
    from core.wallet import sale_acts as A, sale_conditions as S
    from tools.agent_nft.tool import SaleAcceptParams
    s, r = rig, rig.r
    _owner_open(s)
    tid = _fresh_treasury_nft(s)
    buyer, offer, link = _buyer_offer(s, tid)
    dry = _run(_nft_tool(s).agent_nft_sale_accept(SaleAcceptParams(chain=R.CHAIN, offer=link)))
    print("\n  accept (dry, no approval yet):", dry.error or dry.extracted_content)
    assert dry.error is None and "cannot be simulated before" in dry.extracted_content
    weth = lambda a: r.uint(R.WETH_TESTNET, "balanceOf(address)", a)  # noqa: E731
    me0, buyer0, market0 = weth(s.signer.address), weth(buyer.address), weth(r.market)
    res = _run(_nft_tool(s).agent_nft_sale_accept(SaleAcceptParams(chain=R.CHAIN, offer=link,
                                                                   dry_run=False)))
    out = res.error or res.extracted_content
    print("  accept:", out)
    assert res.error is None and out.count("(confirmed)") == 2, out
    import re
    approve_tx, accept_tx = re.findall(r"tx:\s+(0x[0-9a-f]{64})", out)
    assert r.receipt(approve_tx)["to"].lower() == r.collection
    rc = r.receipt(accept_tx)
    assert rc["to"].lower() == r.market
    sold = [lg for lg in rc["logs"] if lg["topics"][0] == S.TOPIC_SOLD]
    assert len(sold) == 1 and int(sold[0]["topics"][2], 16) == tid
    royalty = int(sold[0]["data"][2 + 3 * 64:2 + 4 * 64], 16)
    assert r.owner_of(tid) == buyer.address.lower()
    assert weth(s.signer.address) - me0 == offer.price - royalty
    assert buyer0 - weth(buyer.address) == offer.price and weth(r.market) == market0
    assert A.read_approved(r.core_rpc, r.collection, tid) == S.ZERO
    assert r.uint(R.WETH_TESTNET, "allowance(address,address)", buyer.address, r.market) == 0
    res = _watch(s)
    assert tid in [x["token_id"] for x in res.lost]
    print("  watch:", [t for t in s.told if f"#{tid}" in t][-1])


def test_35_an_expired_listing_is_reported_and_the_cancel_revokes_the_approval(rig):
    from core.wallet import market_orders, sale_acts as A, sale_conditions as S
    from tools.agent_nft.tool import SaleCancelListingParams, SaleListParams
    s, r = rig, rig.r
    _owner_open(s)
    tid = _fresh_treasury_nft(s)
    res = _run(_nft_tool(s).agent_nft_sale_list(SaleListParams(chain=R.CHAIN, nft=str(tid),
                                                               price="0.002", expiry_days=1,
                                                               dry_run=False)))
    assert res.error is None and "LISTED" in res.extracted_content, res.error
    assert A.read_approved(r.core_rpc, r.collection, tid) == r.market
    r.warp(2 * 86_400)
    n = len(s.told)
    _watch(s)
    ended = [t for t in s.told[n:] if "has ENDED: expired" in t and f"#{tid}" in t]
    assert ended, s.told[n:]
    print("\n  watch:", ended[-1])
    import re
    salt = int(re.search(r"salt (\d+);", res.extracted_content).group(1))
    # by its salt (cancel(salt)); the watch already dropped the ended row, so the NFT is named
    res = _run(_nft_tool(s).agent_nft_sale_cancel_listing(SaleCancelListingParams(
        chain=R.CHAIN, salt=salt, nft=str(tid), dry_run=False)))
    out = res.error or res.extracted_content
    print("  cancel + revoke:", out)
    assert res.error is None and out.count("(confirmed)") == 2, out
    assert A.read_approved(r.core_rpc, r.collection, tid) == S.ZERO
    assert r.owner_of(tid) == s.signer.address.lower()


def test_36_an_offer_or_a_seaport_order_is_never_signed(rig):
    """Class O signs a pinned market Listing only: an Offer, a Seaport order and a listing of a
    token the treasury does not hold refuse BEFORE the key is used."""
    from core.wallet import collection_registry, sale_orders as O
    from core.wallet.sign_policy import SignRefused, SignRequest, sign_market_order
    s, r = rig, rig.r
    held = [t for t in s.minted if r.owner_of(t) == s.signer.address.lower()]
    name, version, cid, market = O.read_domain(r.core_rpc, r.market)
    import core.wallet.sale_conditions as S
    now = int(r.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
    base = S.Listing.of({"collection": r.collection, "id": held[-1],
                         "transferCount": r.uint(r.collection, "transferCount(uint256)", held[-1]),
                         "seller": s.signer.address.lower(), "currency": S.ZERO, "price": 1,
                         "maxRoyalty": 0, "expiry": now + 86_400, "salt": 1,
                         "promiseHash": S.conditions_hash([]),     # not the promise core builds
                         "nonce": r.uint(r.market, "nonces(address)", s.signer.address)})
    for mutate, needle in (
            (lambda t: t.update(primaryType="Offer"), "an Offer is not signed"),
            (lambda t: t["types"].update(OrderComponents=[]), "Seaport"),
            (lambda t: t["message"].update(id=10 ** 6), "could not be read"),
            (lambda t: None, "promiseHash")):
        typed = O.typed_listing(base, name=name, version=version, chain_id=cid, market=market)
        mutate(typed)
        try:
            facts = O.read_listing_facts(r.core_rpc, collection_registry, chain=R.CHAIN,
                                         payload=typed, holder=s.signer.address)
        except O.OrderRefused as exc:
            facts = O.ListingFacts(0, refusal=str(exc))
        with pytest.raises(SignRefused) as exc:
            sign_market_order(s.signer, SignRequest("eip712", typed), facts=facts, chain=R.CHAIN)
        print(f"\n  refused: {str(exc.value)[:160]}")
        assert needle in str(exc.value), str(exc.value)
