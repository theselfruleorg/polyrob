"""069 v4 A3 — ``account=`` / ``nft=`` on the money verbs: work FROM an NFT's account.

The verb builds a ``via_account`` intent (the treasury signs ``account.execute(inner, 0)``, or the
approve → spend → reset batch for an ERC-20 swap), hands it to the SAME guard, books the result
against the account and appends a signed journal entry. It refuses — before the guard — unless
the collection is pinned (with its live code hash), the account is the pinned one and deployed,
and the treasury owns the NFT.
"""
import contextlib
import os
import time

import pytest

from core.wallet import abi, erc6551
from core.wallet.nft_account import (NftAccountError, load_journal, parse_nft,
                                     recover_owner, resolve)
from core.wallet.signer import LocalEoaSigner
from core.wallet.tx_guard import Decision
from tests.collection_pins import CODE, pin, profile
from tools.defi.providers.routes import RouteQuote
from tools.defi.trade_tool import DefiTradeTool, SwapParams, TransferParams

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
TO = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
PINNED = "0xC011000000000000000000000000000000000001"
STRANGER = "0x" + "77" * 20
ID = 3
ACCOUNT = erc6551.account_address(8453, PINNED, ID)

SEL_OWNER_OF = abi.selector("ownerOf(uint256)")
SEL_STATE = abi.selector("state()")
SEL_TOKEN = abi.selector("token()")


def _word(v):
    if isinstance(v, str):
        return "0x" + "0" * 24 + v[2:].lower()
    return "0x" + f"{int(v):064x}"


class AcctRpc:
    def __init__(self, owner, *, state=3, account_code="0x" + "60" * 45, code=CODE,
                 token=(8453, PINNED, ID)):
        self.owner, self.state, self.account_code, self.code, self.token = (
            owner, state, account_code, code, token)

    def __call__(self, method, params):
        if method == "eth_getCode":
            return self.code if params[0].lower() == PINNED.lower() else self.account_code
        if method == "eth_call":
            sel = params[0]["data"][:10]
            if sel == SEL_OWNER_OF:
                return _word(self.owner)
            if sel == SEL_STATE:
                return _word(self.state)
            if sel == SEL_TOKEN:
                c, addr, i = self.token
                return "0x" + f"{c:064x}" + "0" * 24 + addr[2:].lower() + f"{i:064x}"
        raise AssertionError(f"unexpected {method} {params}")


class _Gate:
    def __init__(self):
        self.recorded = []

    @contextlib.asynccontextmanager
    async def reserve(self):
        yield

    def record(self, **kw):
        self.recorded.append(kw)


class _Wallet:
    def __init__(self, gate, signer):
        self.policy, self._signer = gate, signer

    def operational_signer(self):
        return self._signer


class _Rail:
    last = None

    def __init__(self, chain, signer, **kw):
        self.sent = False
        _Rail.last = self

    def build_call(self, *, to, data, value=0):
        return {"to": to, "data": data, "value": value, "chainId": 8453, "gas": 120_000}

    def build_erc20_transfer(self, *, token, to, amount_raw):
        return {"to": token, "value": 0, "chainId": 8453, "gas": 120_000,
                "data": abi.encode_call("transfer", [{"type": "address"}, {"type": "uint256"}],
                                        [to, amount_raw])}

    def size_gas(self, tx, used):
        return tx

    def sign_and_send(self, tx):
        self.sent = True
        self.sent_tx = tx
        return "0x" + "cd" * 32

    def await_receipt(self, tx_hash, **kw):
        from core.wallet.broadcast.evm import Receipt
        return Receipt(tx_hash=tx_hash, status="success", block_number=7, gas_used=60_000)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    pin(monkeypatch, profile(PINNED, chain_id=8453, journal_prefix="POLYROB"))
    signer = LocalEoaSigner(os.urandom(32))          # a fresh key, never a well-known one
    return {"signer": signer, "tmp": tmp_path}


def _tool(env, decision=None, *, rpc=None, route_fn=None):
    seen = []
    gate = _Gate()

    def guard(intent, tx, **kw):
        seen.append((intent, tx, kw))
        return decision or Decision(True, "authorized", "autonomous", 0.5, sim_gas_used=90_000)
    tool = DefiTradeTool(wallet=_Wallet(gate, env["signer"]), rail_factory=_Rail, guard_fn=guard,
                         price_fn=lambda c, a: 1.0, route_fn=route_fn,
                         account_rpc=rpc or AcctRpc(env["signer"].address))
    return tool, gate, seen


def _transfer(**kw):
    base = dict(chain="base", token=USDC, to=TO, amount=0.25, max_spend_usd=1.0)
    base.update(kw)
    return TransferParams(**base)


# --- resolve --------------------------------------------------------------------------------

def test_parse_nft_forms():
    assert parse_nft("7") == (None, 7) and parse_nft("#7") == (None, 7)
    assert parse_nft(f"{PINNED}#7") == (PINNED.lower(), 7)
    assert parse_nft(f"{PINNED}/7") == (PINNED.lower(), 7)
    with pytest.raises(NftAccountError):
        parse_nft("POLYROB seven")


def test_resolve_by_id_and_by_account_agree(env):
    rpc = AcctRpc(env["signer"].address)
    a = resolve("base", rpc=rpc, treasury=env["signer"].address, nft=str(ID))
    b = resolve("base", rpc=rpc, treasury=env["signer"].address, account=ACCOUNT)
    assert a == b and a.account == ACCOUNT and a.label == f"POLYROB #{ID}"


@pytest.mark.parametrize("rpc_kw,nft,account,why", [
    (dict(owner=STRANGER), str(ID), None, "does not own"),
    (dict(code="0x6080dead"), str(ID), None, "not the pinned"),
    (dict(account_code="0x"), str(ID), None, "not deployed"),
    (dict(token=(8453, STRANGER, ID)), None, ACCOUNT, "not a pinned collection"),
    (dict(token=(1, PINNED, ID)), None, ACCOUNT, "on chain 1"),
    (dict(), f"{STRANGER}#{ID}", None, "not a pinned collection"),
    (dict(), "99999", None, "outside the collection's ids"),
    (dict(), None, "0x" + "12" * 20, "not the pinned ERC-6551 account"),
])
def test_resolve_refuses(env, rpc_kw, nft, account, why):
    rpc_kw.setdefault("owner", env["signer"].address)
    with pytest.raises(NftAccountError) as e:
        resolve("base", rpc=AcctRpc(**rpc_kw), treasury=env["signer"].address, nft=nft,
                account=account)
    assert why in str(e.value)


def test_resolve_refuses_with_nothing_pinned_or_an_untrusted_registry(env, monkeypatch):
    pin(monkeypatch)
    with pytest.raises(NftAccountError, match="no collection is pinned"):
        resolve("base", rpc=AcctRpc(env["signer"].address), treasury=env["signer"].address, nft="1")
    from core.wallet import collection_registry

    def boom():
        raise collection_registry.CollectionRegistryError("writable")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    with pytest.raises(NftAccountError, match="cannot be trusted"):
        resolve("base", rpc=AcctRpc(env["signer"].address), treasury=env["signer"].address, nft="1")


def test_two_pinned_collections_need_the_collection_named(env, monkeypatch):
    other = "0xC011000000000000000000000000000000000002"
    pin(monkeypatch, profile(PINNED, chain_id=8453), profile(other, chain_id=8453))
    with pytest.raises(NftAccountError, match="name one"):
        resolve("base", rpc=AcctRpc(env["signer"].address), treasury=env["signer"].address, nft="3")
    assert resolve("base", rpc=AcctRpc(env["signer"].address), treasury=env["signer"].address,
                   nft=f"{PINNED}#3").account == ACCOUNT


# --- transfer -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transfer_from_an_account_is_a_via_account_execute_through_the_guard(env):
    tool, gate, seen = _tool(env)
    res = await tool.transfer(_transfer(nft=str(ID)))
    assert res.error is None, res.error
    intent, tx, _kw = seen[0]
    assert tx["to"] == ACCOUNT and int(tx["value"]) == 0
    inner_to, value, inner_data, op = erc6551.decode_execute(tx["data"])
    assert inner_to.lower() == USDC.lower() and value == 0 and op == 0
    assert inner_data.startswith(abi.selector("transfer(address,uint256)"))
    assert intent.via_account == ACCOUNT and intent.via_account_state == 3
    assert intent.via_account_batch is False and intent.to == TO
    assert "from account" in res.extracted_content and "DRY RUN" in res.extracted_content
    assert gate.recorded == []


@pytest.mark.asyncio
async def test_a_sent_transfer_is_booked_against_the_account_and_journaled(env):
    tool, gate, _seen = _tool(env)
    res = await tool.transfer(_transfer(account=ACCOUNT, dry_run=False))
    assert "SENT AND CONFIRMED" in res.extracted_content
    assert gate.recorded[0]["account"].lower() == ACCOUNT.lower()
    assert "journal: entry #0 (tend) signed" in res.extracted_content
    from core.wallet.nft_account import journal_path
    from core.instance import resolve_owner_user_id
    path = journal_path(env["tmp"], resolve_owner_user_id(), None, 8453, ACCOUNT)
    entries = load_journal(path)
    assert len(entries) == 1 and entries[0]["kind"] == "tend" and entries[0]["prev"] == "genesis"
    assert recover_owner(entries[0], "POLYROB") == env["signer"].address.lower()
    assert ("0x" + "cd" * 32) in entries[0]["refs"]
    # a second write chains onto the first
    await tool.transfer(_transfer(account=ACCOUNT, dry_run=False))
    entries = load_journal(path)
    from core.wallet.nft_account import digest
    assert entries[1]["seq"] == 1 and entries[1]["prev"] == digest(entries[0])


@pytest.mark.asyncio
async def test_transfer_refuses_before_the_guard_when_the_treasury_does_not_own_the_nft(env):
    tool, gate, seen = _tool(env, rpc=AcctRpc(STRANGER))
    _Rail.last = None
    res = await tool.transfer(_transfer(nft=str(ID), dry_run=False))
    assert res.error and "does not own" in res.error and "Nothing was broadcast" in res.error
    assert seen == [] and _Rail.last is None


@pytest.mark.asyncio
async def test_account_mode_is_off_without_the_flag(env, monkeypatch):
    monkeypatch.delenv("AGENT_NFT_ENABLED")
    tool, _gate, seen = _tool(env)
    res = await tool.transfer(_transfer(nft=str(ID)))
    assert res.error and "AGENT_NFT_ENABLED" in res.error and seen == []


@pytest.mark.asyncio
async def test_a_plain_transfer_is_unchanged(env):
    tool, gate, seen = _tool(env)
    await tool.transfer(_transfer(dry_run=False))
    intent, tx, _ = seen[0]
    assert intent.via_account is None and tx["to"].lower() == USDC.lower()
    assert gate.recorded[0]["account"] is None


# --- swap -----------------------------------------------------------------------------------

def _route_fn(holders):
    def fn(chain, token_in, token_out, amount_in_raw, *, holder, slippage_bps):
        holders.append(holder)
        return RouteQuote(chain=chain, token_in=token_in, token_out=token_out,
                          amount_in_raw=amount_in_raw, amount_out_raw=10 ** 15,
                          amount_out_min_raw=99 * 10 ** 13, spender=ROUTER, to=ROUTER,
                          calldata="0x12345678", value_raw=(amount_in_raw if token_in == "native" else 0),
                          venue="test", quoted_at=time.time(), locally_built=True), ""
    return fn


@pytest.fixture
def no_side_checks(monkeypatch):
    import tools.defi.identity_gate as ig
    import tools.defi.providers.univ3 as u
    import core.wallet.buy_target as bt
    monkeypatch.setattr(ig, "buy_identity_refusal", lambda **kw: None)
    monkeypatch.setattr(bt, "acquisition_refusal", lambda *a, **k: None)
    monkeypatch.setattr(DefiTradeTool, "_route_sanity",
                        lambda self, *a, **k: ("consistent", "route check: test"))
    monkeypatch.setattr(DefiTradeTool, "_held_balance_raw", lambda self, *a: 10 ** 18)

    def no_allowance_read(*a, **k):
        raise AssertionError("an account swap must not read a standing allowance")
    monkeypatch.setattr(u, "read_allowance", no_allowance_read)


@pytest.mark.asyncio
async def test_an_erc20_swap_from_an_account_is_the_approve_spend_reset_batch(env, no_side_checks):
    holders = []
    tool, gate, seen = _tool(env, route_fn=_route_fn(holders))
    res = await tool.swap(SwapParams(chain="base", token_in=USDC, token_out=WETH, amount_in=1.0,
                                     max_spend_usd=2.0, nft=str(ID), dry_run=False))
    assert res.error is None, res.error
    assert holders == [ACCOUNT]                          # the route pays the ACCOUNT
    intent, tx, _ = seen[0]
    assert tx["to"] == ACCOUNT
    legs = erc6551.decode_execute_batch(tx["data"])
    assert [leg[0].lower() for leg in legs] == [USDC.lower(), ROUTER.lower(), USDC.lower()]
    assert legs[0][2].startswith(abi.selector("approve(address,uint256)"))
    assert intent.via_account == ACCOUNT and intent.via_account_batch is True
    assert intent.token == USDC and intent.to == ROUTER and intent.inflow_token == WETH
    assert gate.recorded[0]["account"].lower() == ACCOUNT.lower()
    assert "journal: entry #0 (entry) signed" in res.extracted_content


@pytest.mark.asyncio
async def test_a_native_swap_from_an_account_is_one_execute_carrying_the_value(env, no_side_checks):
    holders = []
    tool, _gate, seen = _tool(env, route_fn=_route_fn(holders))
    res = await tool.swap(SwapParams(chain="base", token_in="native", token_out=WETH,
                                     amount_in=0.001, max_spend_usd=5.0, account=ACCOUNT))
    assert res.error is None, res.error
    intent, tx, _ = seen[0]
    to, value, data, op = erc6551.decode_execute(tx["data"])
    assert to.lower() == ROUTER.lower() and value == 10 ** 15 and op == 0
    assert int(tx["value"]) == 0
    assert intent.via_account == ACCOUNT and intent.via_account_batch is False and intent.token is None


# --- the read --------------------------------------------------------------------------------

def test_portfolio_reads_the_account(env, monkeypatch):
    from tools.defi import account_mode
    from tools.defi.data_tool import DefiDataTool, PortfolioParams
    monkeypatch.setattr(account_mode, "default_rpc", lambda chain: AcctRpc(env["signer"].address))
    seen = []
    tool = DefiDataTool(holder=env["signer"].address,
                        index_fn=lambda holder, chain: seen.append(holder) or {})
    monkeypatch.setattr(DefiDataTool, "_gas_lines", lambda self, h, c: [])
    res = tool._portfolio_sync(PortfolioParams(chain="base", nft=str(ID)))
    assert res.error is None, res.error
    assert seen == [ACCOUNT] and f"holdings for {ACCOUNT}" in res.extracted_content
    monkeypatch.setattr(account_mode, "default_rpc", lambda chain: AcctRpc(STRANGER))
    res = tool._portfolio_sync(PortfolioParams(chain="base", nft=str(ID)))
    assert res.error and "does not own" in res.error


# --- the optional site publish after a journal append (069 v4 A3; polyrob_drop.publish) ---------

class _PubResult:
    def __init__(self, text):
        self.text = text

    def line(self):
        return self.text


@pytest.fixture
def publisher(monkeypatch):
    """Install a fake ``polyrob_drop.publish`` whose ``publish_journal`` runs *behaviour*."""
    import sys
    import types
    calls = []
    state = {"behaviour": lambda chain_id, account: _PubResult("journal publish: published — 1 new of 1")}

    def publish_journal(chain_id, account):
        calls.append((chain_id, account))
        return state["behaviour"](chain_id, account)
    pkg = types.ModuleType("polyrob_drop")
    mod = types.ModuleType("polyrob_drop.publish")
    mod.publish_journal = publish_journal
    pkg.publish = mod
    monkeypatch.setitem(sys.modules, "polyrob_drop", pkg)
    monkeypatch.setitem(sys.modules, "polyrob_drop.publish", mod)
    return calls, state


def test_publish_helper_is_a_silent_no_op_without_the_package(monkeypatch):
    import sys
    from core.wallet.nft_account import publish_after_append
    monkeypatch.setitem(sys.modules, "polyrob_drop", None)        # import → ImportError
    monkeypatch.setitem(sys.modules, "polyrob_drop.publish", None)
    assert publish_after_append(8453, ACCOUNT) == ""


@pytest.mark.asyncio
async def test_without_the_package_the_verb_result_has_no_publish_line(env, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "polyrob_drop", None)
    monkeypatch.setitem(sys.modules, "polyrob_drop.publish", None)
    tool, _gate, _seen = _tool(env)
    res = await tool.transfer(_transfer(account=ACCOUNT, dry_run=False))
    assert "journal: entry #0 (tend) signed" in res.extracted_content
    assert "journal publish" not in res.extracted_content


@pytest.mark.asyncio
async def test_a_published_journal_line_appears_in_the_verb_result(env, publisher):
    calls, _state = publisher
    tool, _gate, _seen = _tool(env)
    res = await tool.transfer(_transfer(account=ACCOUNT, dry_run=False))
    assert "journal: entry #0 (tend) signed" in res.extracted_content
    assert "journal publish: published — 1 new of 1" in res.extracted_content
    assert calls == [(8453, ACCOUNT.lower())]


@pytest.mark.asyncio
async def test_a_raising_publisher_leaves_the_verb_result_unchanged(env, publisher):
    calls, state = publisher

    def boom(chain_id, account):
        raise RuntimeError("site down")
    state["behaviour"] = boom
    tool, gate, _seen = _tool(env)
    res = await tool.transfer(_transfer(account=ACCOUNT, dry_run=False))
    assert res.error is None
    assert "SENT AND CONFIRMED" in res.extracted_content
    assert "journal: entry #0 (tend) signed" in res.extracted_content
    assert "journal publish: failed — RuntimeError: site down" in res.extracted_content
    assert gate.recorded and len(calls) == 1


@pytest.mark.asyncio
async def test_a_slow_publisher_is_cut_off_at_the_timeout(env, publisher, monkeypatch):
    import threading
    from core.wallet import nft_account
    monkeypatch.setattr(nft_account, "PUBLISH_TIMEOUT_S", 0.2)
    release = threading.Event()
    _calls, state = publisher
    state["behaviour"] = lambda chain_id, account: (release.wait(5), _PubResult("LATE-PUBLISH-LINE"))[1]
    tool, _gate, _seen = _tool(env)
    t0 = time.monotonic()
    try:
        res = await tool.transfer(_transfer(account=ACCOUNT, dry_run=False))
    finally:
        release.set()
    assert time.monotonic() - t0 < 2.0
    assert res.error is None and "SENT AND CONFIRMED" in res.extracted_content
    assert "journal: entry #0 (tend) signed" in res.extracted_content
    assert "journal publish: pending" in res.extracted_content and "LATE-PUBLISH-LINE" not in res.extracted_content


def test_publish_never_runs_when_the_append_failed(env, publisher, monkeypatch):
    calls, _state = publisher
    from core.wallet import nft_account
    from tools.defi import account_mode

    def broken(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(nft_account, "append_journal", broken)
    held = type("H", (), {"chain_id": 8453, "account": ACCOUNT})()
    line = account_mode.journal_line(held, env["signer"], kind="tend", text="x")
    assert "journal: NOT written (disk full)" in line
    assert "journal publish" not in line and calls == []
