"""115 W4: ``lp_collect(protocol='v4')`` — from the wallet, and with ``account=``/``nft=`` from
an NFT's account that owns the position (the wallet signs ``account.execute(PositionManager,
0, modifyLiquidities(...), 0)``). The verb builds; the guard (faked here) judges."""
from types import SimpleNamespace

import pytest

from core.wallet import abi, dex_registry, erc6551, liquidity_guard as G
from core.wallet.tx_guard import Decision
from tools.defi import account_mode, lp_reads as R, lp_v4 as V4, lp_verbs as V
from tools.defi.trade_tool import DefiTradeTool, LpCollectParams, LpRemoveParams
from tests.unit.tools.defi.test_deploy_and_call_verbs import _Gate, _Rail, _Wallet, HOLDER

ROW = dex_registry.row_for("robinhood", "v4")
NPM = ROW.position_manager
ZERO = "0x" + "0" * 40
TKN = "0x" + "55" * 20
ACCT = "0x" + "ac" * 20
KEY = {"currency0": ZERO, "currency1": TKN, "fee": 10000, "tickSpacing": 200, "hooks": ZERO}


@pytest.fixture(autouse=True)
def reads(monkeypatch):
    monkeypatch.setenv(V.FLAG, "true")
    monkeypatch.setattr(dex_registry, "verify_pins", lambda *a: None)
    owner = {"addr": HOLDER}
    monkeypatch.setattr(R, "view", lambda rpc, to, spec, args=None:
                        owner["addr"] if spec["name"] == "ownerOf" else 18)
    monkeypatch.setattr(V4, "position_pool_key", lambda rpc, chain, token_id: dict(KEY))
    return owner


def _receipt():
    return {"logs": []}


def _tool(captured, allow=True, gas=210_000):
    gate = _Gate()

    def guard(intent, tx, **kw):
        captured.append((intent, tx, kw))
        return Decision(allow, "test", "autonomous", 0.01, gas)
    t = DefiTradeTool(wallet=_Wallet(gate), rail_factory=_Rail, guard_fn=guard,
                      price_fn=lambda *a: 1, fallback_price_fn=lambda *a: None)
    t._lp_rpc = lambda *a: _receipt()
    t._notify_tx = lambda *a, **kw: None
    return t, gate


def _params(**kw):
    return LpCollectParams(**(dict(chain="robinhood", protocol="v4", token_id=42) | kw))


@pytest.mark.asyncio
async def test_direct_collect_builds_the_plain_position_manager_call():
    captured = []
    t, gate = _tool(captured)
    res = await t.lp_collect(_params())
    assert "DRY RUN" in res.extracted_content, res.error
    intent, tx, kw = captured[0]
    assert tx["to"] == NPM and tx["value"] == 0
    assert tx["data"].startswith(abi.selector("modifyLiquidities(bytes,uint256)"))
    assert G.decode_v4_collect(tx) == {"token_id": 42, "currency0": ZERO,
                                       "currency1": TKN, "recipient": HOLDER.lower()}
    assert intent.via_account is None and "account_rpc" not in kw
    assert intent.lp_position == (NPM, 42) and intent.lp_position_effect == "hold"
    assert intent.lp_inflows == ((None, 0), (TKN, 0)) and not intent.lp_outflows
    assert intent.idempotency_key
    # the plan the verb built is the one shape the guard allows
    G.structural(intent, dict(tx, chainId=4663), holder=HOLDER)
    assert not _Rail.last.sent and not gate.recorded


@pytest.fixture
def held(monkeypatch):
    h = SimpleNamespace(account=ACCT, chain="robinhood", label="collection #7",
                        journal_log=None)
    calls = []

    def resolve(params, chain, treasury, rpc=None):
        calls.append((params.account, params.nft, chain, treasury))
        return h, None
    monkeypatch.setattr(account_mode, "resolve", resolve)
    monkeypatch.setattr(erc6551, "read_state", lambda rpc, acct: 9)
    monkeypatch.setattr(account_mode, "journal_line", lambda *a, **kw: "\n  journal: test")
    return h, calls


@pytest.mark.asyncio
async def test_account_collect_wraps_the_call_in_account_execute(held, reads):
    h, calls = held
    reads["addr"] = ACCT                       # the ACCOUNT owns the position
    captured = []
    t, gate = _tool(captured)
    res = await t.lp_collect(_params(nft="7"))
    assert "DRY RUN" in res.extracted_content, res.error
    assert "from account: " + ACCT in res.extracted_content
    assert calls == [(None, "7", "robinhood", HOLDER)]
    intent, tx, kw = captured[0]
    assert tx["to"] == ACCT and tx["value"] == 0
    assert tx["data"].startswith(abi.selector("execute(address,uint256,bytes,uint8)"))
    inner_to, value, inner, op = erc6551.decode_execute(tx["data"])
    assert (inner_to.lower(), value, op) == (NPM.lower(), 0, 0)
    assert G.decode_v4_collect({"data": inner})["recipient"] == ACCT.lower()
    assert intent.via_account == ACCT and intent.via_account_state == 9
    assert not intent.via_account_batch and not intent.via_account_journal
    assert kw["holder"] == HOLDER              # the wallet signs; the guard swaps in the account
    assert G.may_ride_account(intent)


@pytest.mark.asyncio
async def test_account_collect_is_sized_and_booked_against_the_account(held, reads):
    reads["addr"] = ACCT
    captured = []
    t, gate = _tool(captured, gas=0)          # no gasUsed: an account call is still sized
    res = await t.lp_collect(_params(account=ACCT, dry_run=False))
    assert "RESULT: success" in res.extracted_content, res.error
    assert _Rail.last.sent
    assert gate.recorded and gate.recorded[-1]["account"] == ACCT
    assert gate.recorded[-1]["action"] == "lp_collect"
    assert "journal: test" in res.extracted_content


@pytest.mark.asyncio
async def test_a_position_the_holder_does_not_own_refuses_before_the_guard(held, reads):
    captured = []
    t, _ = _tool(captured)                     # ownerOf answers the WALLET, not the account
    res = await t.lp_collect(_params(account=ACCT))
    assert "does not own" in res.error and not captured


@pytest.mark.asyncio
async def test_account_on_v3_collect_refuses(held):
    captured = []
    t, _ = _tool(captured)
    res = await t.lp_collect(_params(protocol="v3", account=ACCT))
    assert "protocol='v4' only" in res.error and not captured


@pytest.mark.asyncio
async def test_v4_remove_still_refuses():
    captured = []
    t, _ = _tool(captured)
    res = await t.lp_remove(LpRemoveParams(chain="robinhood", token_id=42, protocol="v4"))
    assert "withdrawal is not built" in res.error and not captured
    assert "account" not in LpRemoveParams.model_fields


@pytest.mark.asyncio
async def test_an_unpinned_hook_refuses_in_the_verb(monkeypatch):
    monkeypatch.setattr(V4, "position_pool_key",
                        lambda *a: dict(KEY, hooks="0x" + "1" * 40))
    captured = []
    t, _ = _tool(captured)
    res = await t.lp_collect(_params())
    assert "no pinned code hash" in res.error and not captured
