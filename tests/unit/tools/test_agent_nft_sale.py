"""``agent_nft_sale_claim`` / ``agent_nft_sale_settle`` and the sale lines of the account view
(proposal 115 W8, item 5). The guard shape is pinned in tests/unit/core/wallet/test_collection_sale.py."""
import asyncio
import types

import pytest

from core.wallet import collection_registry
from core.wallet import collection_sale as S
from core.wallet.policy import PolicyGate
from tools.agent_nft import sale as V
from tools.agent_nft.tool import AgentNftTool, SaleClaimParams, SaleSettleParams
from tests.collection_pins import CODE, pin, profile, sale, sale_address
from tests.unit.tools.test_agent_nft_collection_reveal import FakeRail

COLLECTION = "0xc011000000000000000000000000000000000001"
SALE = sale_address(COLLECTION)
TREASURY = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"
CLEARING = 66 * 10 ** 15


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _err(res):
    return getattr(res, "error", None)


def _text(res):
    return getattr(res, "extracted_content", None) or ""


def _w(n):
    return format(int(n), "064x")


def _aw(a):
    return "0" * 24 + a.lower()[2:]


class Chain:
    """A fake RPC over the pinned collection and its sale."""

    def __init__(self, *, records=None, settled=True, owed=0, phase=3, unreadable=(), code=CODE):
        self.records = records if records is not None else {
            0: (TREASURY, 2, 70 * 10 ** 15, False), 1: (OTHER, 1, 80 * 10 ** 15, False),
            2: (TREASURY, 1, 99 * 10 ** 15, True), 3: (TREASURY, 1, CLEARING, False)}
        self.settled, self.owed, self.phase = settled, owed, phase
        self.unreadable = set(unreadable)
        self.code = code

    def __call__(self, method, params, *a, **k):
        if method == "eth_getCode":
            return self.code
        if method == "eth_blockNumber":
            return hex(500)
        if method == "eth_getLogs":
            assert params[0]["address"] == SALE and params[0]["topics"] == [S.TOPIC_BID]
            return [{"address": SALE, "topics": [S.TOPIC_BID], "blockNumber": hex(10 + i),
                     "logIndex": "0x0",
                     "data": "0x" + _w(i) + _aw(who) + _w(100 + i) + _w(qty) + _w(paid)}
                    for i, (who, qty, paid, _d) in self.records.items()]
        assert method == "eth_call", method
        to, data = params[0]["to"].lower(), params[0]["data"].lower()
        assert to == SALE, to
        if "owed" in self.unreadable and data.startswith(S.OWED_SELECTOR):
            raise RuntimeError("timeout")
        if data == S.SETTLED_SELECTOR:
            return "0x" + _w(1 if self.settled else 0)
        if data == S.PHASE_SELECTOR:
            return "0x" + _w(self.phase)
        if data == S.CLEARING_SELECTOR:
            return "0x" + _w(CLEARING)
        if data.startswith(S.OWED_SELECTOR):
            return "0x" + _w(self.owed if data.endswith(TREASURY[2:]) else 0)
        if data.startswith(S.RECORD_SELECTOR):
            i = int(data[10:], 16)
            if i in self.unreadable:
                raise RuntimeError("timeout")
            who, qty, paid, done = self.records[i]
            return "0x" + _aw(who) + _w(100 + i) + _w(qty) + _w(paid) + _w(1 if done else 0)
        raise AssertionError(data)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    pin(monkeypatch, COLLECTION)
    FakeRail.sent = []


def _tool(chain, *, decision=None, seen=None):
    from core.wallet.tx_guard import Decision
    seen = seen if seen is not None else {}

    def guard(intent, tx, **kw):
        seen["intent"], seen["tx"], seen["kw"] = intent, tx, kw
        return decision or Decision(True, "authorized", lane="autonomous", amount_usd=0.02,
                                    sim_gas_used=100_000)
    signer = types.SimpleNamespace(address=TREASURY)
    wallet = types.SimpleNamespace(operational_signer=lambda: signer,
                                   policy=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0))
    return AgentNftTool(wallet=wallet, guard_fn=guard, rpc_fn=chain, rail_factory=FakeRail), seen


# --- the gates ----------------------------------------------------------------------------

def test_the_verbs_are_off_without_agent_nft_enabled(monkeypatch):
    monkeypatch.delenv("AGENT_NFT_ENABLED", raising=False)
    tool, _ = _tool(Chain(owed=5))
    assert "AGENT_NFT_ENABLED" in _err(_run(tool.agent_nft_sale_claim(SaleClaimParams(chain="robinhood"))))
    assert "AGENT_NFT_ENABLED" in _err(_run(tool.agent_nft_sale_settle(SaleSettleParams(chain="robinhood"))))


def test_the_param_models_forbid_unknown_keys():
    with pytest.raises(Exception):
        SaleClaimParams(chain="robinhood", who=OTHER)
    with pytest.raises(Exception):
        SaleSettleParams(chain="robinhood", records=list(range(21)))


def test_no_pinned_sale_refuses(armed, monkeypatch):
    pin(monkeypatch, profile(COLLECTION, capabilities=("reveal",)))
    tool, seen = _tool(Chain(owed=5))
    assert "no collection with a pinned sale" in _err(
        _run(tool.agent_nft_sale_claim(SaleClaimParams(chain="robinhood"))))
    assert "intent" not in seen


def test_an_untrusted_registry_refuses(armed, monkeypatch):
    def boom():
        raise collection_registry.CollectionRegistryError("unknown spec")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    tool, _ = _tool(Chain(owed=5))
    assert "cannot be trusted" in _err(_run(tool.agent_nft_sale_settle(SaleSettleParams(chain="robinhood"))))


def test_a_changed_sale_code_refuses_before_any_read(armed, monkeypatch):
    pin(monkeypatch, profile(COLLECTION, sale_pin=sale(COLLECTION, runtime_sha256="ab" * 32)))
    tool, seen = _tool(Chain(owed=5))
    assert "pinned sale" in _err(_run(tool.agent_nft_sale_claim(SaleClaimParams(chain="robinhood"))))
    assert "intent" not in seen


# --- claim --------------------------------------------------------------------------------

def test_the_claim_intent_is_the_claim_shape_on_the_sale(armed):
    tool, seen = _tool(Chain(owed=7_000))
    res = _run(tool.agent_nft_sale_claim(SaleClaimParams(chain="robinhood")))
    intent, tx = seen["intent"], seen["tx"]
    assert intent.is_claim and intent.min_native_inflow_wei == 7_000
    assert (intent.to, intent.amount_raw, intent.token, intent.via_account) == (SALE, 0, None, None)
    assert tx["to"] == SALE and tx["value"] == 0
    assert S.decode_call(tx["data"]) == ("claim", TREASURY)
    assert seen["kw"]["holder"] == TREASURY
    assert "DRY RUN" in _text(res) and not FakeRail.sent
    assert "NOT the value received" in _text(res)


def test_nothing_owed_sends_nothing(armed):
    tool, seen = _tool(Chain(owed=0))
    res = _run(tool.agent_nft_sale_claim(SaleClaimParams(chain="robinhood", dry_run=False)))
    assert "nothing is owed" in _text(res) and "intent" not in seen and not FakeRail.sent


def test_an_unreadable_owed_is_not_nothing_owed(armed):
    tool, seen = _tool(Chain(owed=5, unreadable={"owed"}))
    err = _err(_run(tool.agent_nft_sale_claim(SaleClaimParams(chain="robinhood"))))
    assert "not evidence that nothing is owed" in err and "intent" not in seen


def test_a_live_claim_sends_and_books_the_verb(armed):
    tool, _ = _tool(Chain(owed=5))
    res = _run(tool.agent_nft_sale_claim(SaleClaimParams(chain="robinhood", dry_run=False)))
    assert len(FakeRail.sent) == 1 and "confirmed" in _text(res)
    assert tool._get_wallet().policy.audit_log[-1]["action"] == "agent_nft_sale_claim"


def test_a_guard_refusal_sends_nothing(armed):
    from core.wallet.tx_guard import Decision
    tool, _ = _tool(Chain(owed=5), decision=Decision(False, "refused: x"))
    res = _run(tool.agent_nft_sale_claim(SaleClaimParams(chain="robinhood", dry_run=False)))
    assert "NOT SENT" in _text(res) and not FakeRail.sent


# --- settle -------------------------------------------------------------------------------

def test_the_settle_names_only_the_treasurys_open_records_and_their_refund(armed):
    tool, seen = _tool(Chain())
    res = _run(tool.agent_nft_sale_settle(SaleSettleParams(chain="robinhood")))
    intent, tx = seen["intent"], seen["tx"]
    # 0: refund (0.070-0.066)×2; 1 another buyer's; 2 done; 3 paid the clearing price (refund 0)
    assert S.decode_call(tx["data"]) == ("settle", [0, 3])
    assert intent.is_claim and intent.min_native_inflow_wei == 2 * (70 - 66) * 10 ** 15
    assert (intent.to, tx["value"], intent.amount_raw) == (SALE, 0, 0)
    assert "DRY RUN" in _text(res)


def test_a_sale_not_settled_sends_nothing(armed):
    tool, seen = _tool(Chain(settled=False, phase=2))
    res = _run(tool.agent_nft_sale_settle(SaleSettleParams(chain="robinhood", dry_run=False)))
    assert "not settled (phase 2)" in _text(res) and "intent" not in seen


def test_records_that_refund_nothing_send_nothing(armed):
    tool, seen = _tool(Chain(records={3: (TREASURY, 1, CLEARING, False)}))
    res = _run(tool.agent_nft_sale_settle(SaleSettleParams(chain="robinhood", dry_run=False)))
    assert "refund nothing" in _text(res) and "intent" not in seen


def test_no_open_record_sends_nothing(armed):
    tool, seen = _tool(Chain(records={1: (OTHER, 1, 80 * 10 ** 15, False)}))
    res = _run(tool.agent_nft_sale_settle(SaleSettleParams(chain="robinhood")))
    assert "none of this treasury is open" in _text(res) and "intent" not in seen


def test_a_named_record_of_another_buyer_refuses(armed):
    tool, seen = _tool(Chain())
    err = _err(_run(tool.agent_nft_sale_settle(SaleSettleParams(chain="robinhood", records=[0, 1]))))
    assert "belongs to" in err and "intent" not in seen


def test_an_unreadable_record_is_never_skipped(armed):
    tool, seen = _tool(Chain(unreadable={3}))
    err = _err(_run(tool.agent_nft_sale_settle(SaleSettleParams(chain="robinhood"))))
    assert "could not be read" in err and "intent" not in seen


def test_more_than_twenty_open_records_settle_twenty_now(armed):
    records = {i: (TREASURY, 1, 70 * 10 ** 15, False) for i in range(25)}
    tool, seen = _tool(Chain(records=records))
    res = _run(tool.agent_nft_sale_settle(SaleSettleParams(chain="robinhood")))
    assert S.decode_call(seen["tx"]["data"])[1] == list(range(20))
    assert "5 more for the next call" in _text(res)


# --- the view lines ----------------------------------------------------------------------

def test_the_sale_lines_show_phase_settled_and_owed(armed):
    prof = collection_registry.profile_for(4663, COLLECTION)
    lines = "\n".join(V.sale_lines(Chain(owed=9, phase=3), prof, treasury=TREASURY, account=OTHER))
    assert "phase 3 (settled), settled True" in lines
    assert "9 wei to the treasury" in lines and "0 wei to this account" in lines


def test_an_unreadable_owed_is_shown_as_unreadable(armed):
    prof = collection_registry.profile_for(4663, COLLECTION)
    lines = "\n".join(V.sale_lines(Chain(unreadable={"owed"}), prof, treasury=TREASURY))
    assert "unreadable to the treasury" in lines and "not 'nothing owed'" in lines


def test_a_sale_with_changed_code_is_not_read(armed, monkeypatch):
    pin(monkeypatch, profile(COLLECTION, sale_pin=sale(COLLECTION, runtime_sha256="ab" * 32)))
    prof = collection_registry.profile_for(4663, COLLECTION)
    lines = V.sale_lines(Chain(), prof, treasury=TREASURY)
    assert len(lines) == 1 and "NOT READ" in lines[0]


def test_a_profile_without_a_sale_has_no_sale_lines(armed, monkeypatch):
    pin(monkeypatch, profile(COLLECTION, capabilities=("reveal",)))
    assert V.sale_lines(Chain(), collection_registry.profile_for(4663, COLLECTION)) == []
