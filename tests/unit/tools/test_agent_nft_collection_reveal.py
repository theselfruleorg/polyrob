"""``agent_nft_collection_reveal`` — due-id selection and the verb's gates (the collection
revealer). The guard shape itself is pinned in
tests/unit/core/wallet/test_tx_guard_collection_reveal.py."""
import asyncio
import types

import pytest

from core.wallet import abi, collection_registry, onchain
from core.wallet import collection_reveal as R
from core.wallet.broadcast.evm import Receipt
from core.wallet.policy import PolicyGate
from tools.agent_nft import reveal as V
from tools.agent_nft.tool import AgentNftTool, RevealParams
from tests.collection_pins import pin, profile

COLLECTION = "0xc011000000000000000000000000000000000001"
TREASURY = "0x2222222222222222222222222222222222222222"
SEL = {k: abi.selector(k) for k in ("nextId()", "nextToReveal()", "revealBlock(uint256)")}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _err(res):
    return getattr(res, "error", None)


def _text(res):
    return getattr(res, "extracted_content", None) or ""


class Chain:
    """A fake RPC over one collection: ``blocks[id] = revealBlock`` (0 = revealed)."""

    def __init__(self, head, blocks, next_to_reveal=1, unreadable=(), receipt_logs=None):
        self.head, self.blocks, self.ntr = head, dict(blocks), next_to_reveal
        self.unreadable = set(unreadable)
        self.receipt_logs = receipt_logs or []
        self.calls = []

    def __call__(self, method, params, *a, **k):
        self.calls.append(method)
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1", "logs": self.receipt_logs}
        assert method == "eth_call", method
        to, data = params[0]["to"].lower(), params[0]["data"].lower()
        if to == onchain.MULTICALL3.lower() and data == "0x42cbb15c":
            return "0x" + format(self.head, "064x")
        if to == onchain.MULTICALL3.lower():
            from eth_abi import decode as _decode
            (calls,) = _decode(["(address,bool,bytes)[]"], bytes.fromhex(data[10:]))
            pairs = []
            for target, _allow, cd in calls:
                cd = "0x" + cd.hex()
                assert target.lower() == COLLECTION and cd[:10] == SEL["revealBlock(uint256)"]
                i = int(cd[10:], 16)
                pairs.append((i not in self.unreadable, None if i in self.unreadable
                              else self.blocks.get(i, 0)))
            return onchain._encode_aggregate3_result(pairs)
        assert to == COLLECTION
        if data == SEL["nextId()"]:
            return "0x" + format(max(self.blocks, default=0) + 1, "064x")
        if data == SEL["nextToReveal()"]:
            return "0x" + format(self.ntr, "064x")
        raise AssertionError(data)


# --- due-id selection -------------------------------------------------------------

def test_due_ids_are_unrevealed_ids_whose_block_has_passed_in_id_order():
    chain = Chain(head=100, blocks={1: 0, 2: 99, 3: 100, 4: 101, 5: 50, 6: 0, 7: 98})
    due, ntr, nid = V.due_ids(chain, COLLECTION, head=100, max_ids=10)
    assert due == [2, 5, 7]           # 1, 6 revealed; 3 (== head) and 4 not due yet
    assert (ntr, nid) == (1, 8)


def test_the_scan_starts_at_next_to_reveal_and_stops_at_the_cap():
    blocks = {i: 10 for i in range(1, 31)}
    due, _ntr, _nid = V.due_ids(Chain(head=100, blocks=blocks, next_to_reveal=5), COLLECTION,
                                head=100, max_ids=10)
    assert due == list(range(5, 15))


def test_an_expired_block_is_still_due_so_it_re_commits_rather_than_being_skipped():
    """Older than 256 L1 blocks: reveal() re-commits it (SPEC §5) — never skipped (rules §7.5)."""
    due, *_ = V.due_ids(Chain(head=10_000, blocks={1: 5}), COLLECTION, head=10_000, max_ids=10)
    assert due == [1]


def test_an_unreadable_reveal_block_stops_the_run():
    with pytest.raises(V.RevealError, match="never skipped"):
        V.due_ids(Chain(head=100, blocks={1: 5, 2: 5}, unreadable={1}), COLLECTION, head=100, max_ids=10)


def test_a_long_queue_is_read_in_chunks():
    blocks = {i: 0 for i in range(1, 451)}
    blocks[449] = 3
    chain = Chain(head=100, blocks=blocks)
    due, *_ = V.due_ids(chain, COLLECTION, head=100, max_ids=10)
    assert due == [449]
    assert chain.calls.count("eth_call") == 2 + 3          # nextToReveal, nextId, 3 chunks of ≤200


def test_head_is_the_evm_block_number_not_eth_block_number():
    chain = Chain(head=777, blocks={})
    assert V.read_head(chain) == 777
    assert "eth_blockNumber" not in chain.calls


# --- the verb ---------------------------------------------------------------------

class FakeRail:
    sent = []

    def __init__(self, chain, signer):
        self.chain = chain

    def build_call(self, *, to, data, value=0):
        return {"to": to, "data": data, "value": value, "chainId": 4663, "nonce": 1,
                "gas": 400_000, "maxFeePerGas": 10 ** 8, "maxPriorityFeePerGas": 1, "type": 2}

    def size_gas(self, tx, used):
        return {**tx, "gas": used * 3 // 2}

    def sign_and_send(self, tx):
        FakeRail.sent.append(tx)
        return "0x" + "ab" * 32

    def await_receipt(self, tx_hash):
        return Receipt(tx_hash=tx_hash, status="success", block_number=5, gas_used=100_000)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    monkeypatch.delenv(V.MAX_GAS_FLAG, raising=False)
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
    return AgentNftTool(wallet=wallet, guard_fn=guard, rpc_fn=chain, rail_factory=FakeRail,
                    price_fn=lambda c, a: 4000.0), seen


def test_the_verb_is_off_without_agent_nft_enabled(monkeypatch):
    monkeypatch.delenv("AGENT_NFT_ENABLED", raising=False)
    tool, _ = _tool(Chain(head=100, blocks={1: 5}))
    assert "AGENT_NFT_ENABLED" in _err(_run(tool.agent_nft_collection_reveal(RevealParams())))


def test_the_verb_needs_no_agent_nft_package(armed):
    tool, _ = _tool(Chain(head=100, blocks={1: 0}))
    tool._impl = lambda: None                                   # package absent
    res = _run(tool.agent_nft_collection_reveal(RevealParams()))
    assert _err(res) is None and "nothing to reveal" in _text(res)


def test_no_pinned_collection_refuses(armed, monkeypatch):
    pin(monkeypatch)
    tool, _ = _tool(Chain(head=100, blocks={1: 5}))
    assert "no collection with the reveal capability is pinned" in _err(
        _run(tool.agent_nft_collection_reveal(RevealParams())))


def test_a_collection_without_the_reveal_capability_is_not_revealed(armed, monkeypatch):
    pin(monkeypatch, profile(COLLECTION, capabilities=("mint",)))
    tool, _ = _tool(Chain(head=100, blocks={1: 5}))
    assert "reveal capability" in _err(_run(tool.agent_nft_collection_reveal(RevealParams())))


def test_an_untrusted_registry_refuses(armed, monkeypatch):
    def boom():
        raise collection_registry.CollectionRegistryError("unknown spec")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    tool, _ = _tool(Chain(head=100, blocks={1: 5}))
    assert "cannot be trusted" in _err(_run(tool.agent_nft_collection_reveal(RevealParams())))


def test_nothing_due_sends_nothing(armed):
    tool, seen = _tool(Chain(head=100, blocks={1: 0, 2: 100}))
    res = _run(tool.agent_nft_collection_reveal(RevealParams(dry_run=False)))
    assert "nothing to reveal" in _text(res) and "intent" not in seen and not FakeRail.sent


def test_the_intent_is_the_reveal_shape_and_goes_through_the_guard(armed):
    tool, seen = _tool(Chain(head=100, blocks={1: 0, 2: 7, 3: 9, 4: 200}))
    res = _run(tool.agent_nft_collection_reveal(RevealParams()))
    intent, tx = seen["intent"], seen["tx"]
    assert intent.is_collection_reveal and tuple(intent.reveal_ids) == (2, 3)
    assert (intent.to, intent.amount_raw, intent.token) == (COLLECTION, 0, None)
    assert tx["to"] == COLLECTION and tx["value"] == 0 and R.decode_reveal(tx["data"]) == [2, 3]
    assert intent.max_spend_usd == V.DEFAULT_MAX_GAS_USD
    assert seen["kw"]["holder"] == TREASURY
    assert "DRY RUN" in _text(res) and not FakeRail.sent
    assert "gas only" in _text(res)                                   # says what it spends


def test_max_spend_is_clamped_to_the_flag(armed, monkeypatch):
    monkeypatch.setenv(V.MAX_GAS_FLAG, "0.05")
    tool, seen = _tool(Chain(head=100, blocks={1: 7}))
    _run(tool.agent_nft_collection_reveal(RevealParams(max_spend_usd=5.0)))
    assert seen["intent"].max_spend_usd == 0.05


def test_a_zero_cap_disables_the_verb(armed, monkeypatch):
    monkeypatch.setenv(V.MAX_GAS_FLAG, "0")
    tool, seen = _tool(Chain(head=100, blocks={1: 7}))
    assert "disabled" in _err(_run(tool.agent_nft_collection_reveal(RevealParams())))


def test_a_guard_refusal_sends_nothing(armed):
    from core.wallet.tx_guard import Decision
    tool, _ = _tool(Chain(head=100, blocks={1: 7}), decision=Decision(False, "refused: x"))
    res = _run(tool.agent_nft_collection_reveal(RevealParams(dry_run=False)))
    assert "NOT SENT" in _text(res) and not FakeRail.sent


def test_a_live_run_sends_and_reports_a_recommit_as_an_alert(armed):
    logs = [{"address": COLLECTION, "topics": [R.TOPIC_REVEALED, "0x" + format(1, "064x")]},
            {"address": COLLECTION, "topics": [R.TOPIC_RECOMMITTED, "0x" + format(2, "064x")]}]
    tool, _ = _tool(Chain(head=100, blocks={1: 7, 2: 8}, receipt_logs=logs))
    res = _run(tool.agent_nft_collection_reveal(RevealParams(dry_run=False)))
    text = _text(res)
    assert len(FakeRail.sent) == 1 and "confirmed" in text
    assert "revealed 1 [1]" in text and "ALERT agent_nft_collection_reveal: 1 id(s) RE-COMMITTED [2]" in text
    assert tool._get_wallet().policy.audit_log[-1]["action"] == "agent_nft_collection_reveal"


def test_the_owner_pause_holds_a_live_reveal(armed, monkeypatch):
    from core.money import authority
    monkeypatch.setattr(authority, "spend_pause_refusal", lambda entry=False: "refused: paused")
    tool, seen = _tool(Chain(head=100, blocks={1: 7}))
    res = _run(tool.agent_nft_collection_reveal(RevealParams(dry_run=False)))
    assert "paused" in _err(res) and "intent" not in seen


def test_the_trading_entry_pause_does_not(armed, monkeypatch):
    from core.money import authority
    monkeypatch.setattr(authority, "spend_pause_refusal",
                        lambda entry=False: "refused: entry paused" if entry else None)
    tool, seen = _tool(Chain(head=100, blocks={1: 7}))
    _run(tool.agent_nft_collection_reveal(RevealParams(dry_run=False)))
    assert "intent" in seen


def test_the_record_books_the_fee_actually_paid_not_the_worst_case():
    tx = {"gas": 150_000, "maxFeePerGas": 10 ** 9}
    rpc = lambda m, p: {"gasUsed": hex(100_000), "effectiveGasPrice": hex(10 ** 8)}  # noqa: E731
    assert V._paid_usd(rpc, "0xh", tx, 0.60) == pytest.approx(0.60 * 10 ** 13 / (15 * 10 ** 13))
    assert V._paid_usd(lambda m, p: {}, "0xh", tx, 0.60) == 0.60           # unreadable: worst case
    over = lambda m, p: {"gasUsed": hex(200_000), "effectiveGasPrice": hex(10 ** 9)}  # noqa: E731
    assert V._paid_usd(over, "0xh", tx, 0.60) == 0.60                       # impossible: worst case
