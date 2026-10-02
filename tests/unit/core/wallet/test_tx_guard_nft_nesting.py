"""W2 (core handoff) — the guard's mirror of the collection rule "no token inside an account of
its own collection".

`transferFrom(alice, accountOf(id), id)` bricks a token and its account forever: AccountV3
refuses its own token only in `onERC721Received`, which `transferFrom` never calls, and two
tokens can own each other. The contract refuses it in `_update`; `tx_guard` refuses the same
move for every NFT the guarded wallet sends, whatever verb built it, by reading the
destination's code (the 173-byte ERC-6551 clone + footer) — fail closed.
"""
import pytest

from core.wallet import collection_registry, erc6551, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from tests.collection_pins import CODE, SUPPLY, pin, profile

HOLDER = "0x1111111111111111111111111111111111111111"
NFT = "0x4444444444444444444444444444444444444444"
OTHER_NFT = "0x4545454545454545454545454545454545454545"
PINNED_A = "0xC011000000000000000000000000000000000001"
PINNED_B = "0xC011000000000000000000000000000000000002"
DEST = "0x9999999999999999999999999999999999999999"


def _clone(contract, token_id=1, chain_id=4663, impl=erc6551.ACCOUNT_V3_IMPL):
    footer = (0).to_bytes(32, "big") + chain_id.to_bytes(32, "big") + b"\x00" * 12 \
        + bytes.fromhex(contract[2:]) + token_id.to_bytes(32, "big")
    return ("0x363d3d373d3d3d363d73" + impl[2:].lower() + "5af43d82803e903d91602b57fd5bf3"
            + footer.hex())


class CodeRpc:
    def __init__(self, code="0x", broken=False):
        self.code, self.broken, self.reads = code, broken, []

    def __call__(self, method, params):
        # 069 v4 A3/§5: a pinned token's move also reads the collection's code (its
        # runtime_sha256) and scans its account's approvals — answered here, not recorded.
        if method == "eth_blockNumber":
            return hex(0x100)
        if method == "eth_getLogs":
            return []
        assert method == "eth_getCode", method
        if str(params[0]).lower() in (PINNED_A.lower(), PINNED_B.lower()):
            return CODE
        self.reads.append(params[0])
        if self.broken:
            raise RuntimeError("rpc down")
        return self.code


def _run(contract, dest, rpc, *, chain="robinhood"):
    intent = tx_guard.TxIntent(chain=chain, token=None, to=contract, amount_raw=0, max_spend_usd=5.0,
                               idempotency_key="k", is_nft_op=True, nft_out=((contract, "erc721", 42, 1),))
    deltas = Deltas(ok=True, native_delta=0, gas_used=90_000,
                    holder_nft_out=((contract.lower(), "erc721", dest.lower(), 42, 1),))
    tx = {"to": contract, "data": "0x23b872dd", "value": 0, "chainId": 4663, "nonce": 1,
          "gas": 120_000, "maxFeePerGas": 10 ** 8}
    return tx_guard.authorize(
        intent, tx, holder=HOLDER, gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=None, simulate_fn=lambda **_: deltas, price_fn=lambda c, a: 3000.0,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda c, t: False, account_rpc=rpc)


def test_a_token_into_an_account_of_its_own_collection_refuses():
    d = _run(NFT, DEST, CodeRpc(_clone(NFT, token_id=42)))        # the self-cycle
    assert d.allowed is False and "its own collection" in d.reason, d.reason
    d = _run(NFT, DEST, CodeRpc(_clone(NFT, token_id=7)))         # a nesting that can close a loop
    assert d.allowed is False and "its own collection" in d.reason


def test_any_implementation_counts_not_only_account_v3():
    d = _run(NFT, DEST, CodeRpc(_clone(NFT, impl="0x" + "ab" * 20)))
    assert d.allowed is False and "its own collection" in d.reason


def test_a_token_into_an_account_of_another_collection_is_a_deposit():
    rpc = CodeRpc(_clone(OTHER_NFT))
    d = _run(NFT, DEST, rpc)
    assert d.allowed is True, d.reason
    assert rpc.reads == [DEST.lower()]


def test_a_pinned_token_into_an_account_of_another_pinned_collection_refuses(monkeypatch):
    pin(monkeypatch, PINNED_A, PINNED_B)
    d = _run(PINNED_A, DEST, CodeRpc(_clone(PINNED_B)))
    assert d.allowed is False and "its own collection" in d.reason
    # an ordinary NFT into a pinned collection's account stays a deposit
    assert _run(NFT, DEST, CodeRpc(_clone(PINNED_B))).allowed is True


def test_an_untrusted_registry_fails_closed(monkeypatch):
    def boom():
        raise collection_registry.CollectionRegistryError("unknown capability")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    d = _run(NFT, DEST, CodeRpc("0x"))
    assert d.allowed is False and "cannot be trusted" in d.reason


def test_an_untrusted_registry_does_not_stop_a_transaction_without_an_nft_move(monkeypatch):
    # owner decision 2026-10-01: a broken registry refuses NFT moves and collection calls only
    def boom():
        raise collection_registry.CollectionRegistryError("unknown capability")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    assert tx_guard._collection_account_destination_refusal(
        "robinhood", {"to": DEST, "data": "0xa9059cbb" + "00" * 64}, ()) is None
    assert "cannot be trusted" in tx_guard._collection_account_destination_refusal(
        "robinhood", {"to": DEST, "data": "0x"}, [(NFT, "erc721", DEST, 1, 1)])


def test_an_account_bound_to_another_chain_loses_the_token():
    d = _run(NFT, DEST, CodeRpc(_clone(OTHER_NFT, chain_id=8453)))
    assert d.allowed is False and "owner() is address(0)" in d.reason


def test_an_eoa_or_ordinary_contract_destination_passes():
    assert _run(NFT, DEST, CodeRpc("0x")).allowed is True
    assert _run(NFT, DEST, CodeRpc("0x6080604052" + "00" * 200)).allowed is True


def test_an_unreadable_destination_fails_closed():
    d = _run(NFT, DEST, CodeRpc(broken=True))
    assert d.allowed is False and "failing closed" in d.reason


def test_a_burn_reads_nothing():
    rpc = CodeRpc(broken=True)
    d = _run(NFT, "0x000000000000000000000000000000000000dEaD", rpc)
    assert rpc.reads == [] and "failing closed" not in (d.reason or "")


def test_read_clone_shapes():
    rpc = CodeRpc(_clone(NFT, token_id=9, chain_id=46630))
    impl, salt, chain_id, contract, token_id = erc6551.read_clone(rpc, DEST)
    assert (impl.lower(), salt, chain_id, contract.lower(), token_id) == \
        (erc6551.ACCOUNT_V3_IMPL.lower(), 0, 46630, NFT.lower(), 9)
    assert erc6551.read_clone(CodeRpc("0x"), DEST) is None
    assert erc6551.read_clone(CodeRpc(_clone(NFT)[:-2]), DEST) is None     # 172 bytes
    with pytest.raises(RuntimeError):
        erc6551.read_clone(CodeRpc(broken=True), DEST)


# --- an account NOT DEPLOYED YET: no code to read, so pure CREATE2 math (lead follow-up) ------
# The contract's OwnershipCycle check reads the destination's code; accountOf(k) for a token k
# not minted yet has none, so a pinned token sent (or minted) there passes and later sits inside
# token k's account. The guard refuses every such address, computed offline for ids up to the
# profile's max_supply.

def _pinned(monkeypatch, **kw):
    pin(monkeypatch, profile(PINNED_A, **kw))


def test_the_offline_set_is_the_registry_math():
    s = erc6551.collection_account_addresses(4663, PINNED_A, SUPPLY)
    assert len(s) == SUPPLY + 1
    for k in (1, 2, 100, SUPPLY):
        assert erc6551.account_address(4663, PINNED_A, k).lower() in s
    assert erc6551.account_address(46630, PINNED_A, 1).lower() not in s
    assert erc6551.collection_account_addresses(
        4663, PINNED_A.upper().replace("0X", "0x"), SUPPLY) is s  # cached


def test_the_offline_set_follows_the_profiles_max_supply(monkeypatch):
    _pinned(monkeypatch, max_supply=10)
    inside = erc6551.account_address(4663, PINNED_A, 10)
    beyond = erc6551.account_address(4663, PINNED_A, 11)
    assert _run(PINNED_A, inside, CodeRpc("0x")).allowed is False
    assert _run(PINNED_A, beyond, CodeRpc("0x")).allowed is True


def test_a_pinned_token_into_an_undeployed_account_of_its_collection_refuses(monkeypatch):
    _pinned(monkeypatch)
    future = erc6551.account_address(4663, PINNED_A, 4242)
    rpc = CodeRpc("0x")                                   # no code there yet
    d = _run(PINNED_A, future, rpc)
    assert d.allowed is False and "deployed or not" in d.reason, d.reason
    assert rpc.reads == []                                # decided without a read


def test_an_ordinary_nft_into_the_account_of_an_unminted_pinned_token_refuses(monkeypatch):
    """C5: whoever mints #4242 later owns what sits in its account — any asset, an ordinary NFT
    too (``ownerOf`` cannot be read here, which also refuses)."""
    _pinned(monkeypatch)
    d = _run(NFT, erc6551.account_address(4663, PINNED_A, 4242), CodeRpc("0x"))
    assert d.allowed is False and "not minted yet" in d.reason, d.reason


def test_a_pinned_token_to_an_ordinary_address_passes(monkeypatch):
    _pinned(monkeypatch)
    assert _run(PINNED_A, DEST, CodeRpc("0x")).allowed is True


def _mint_to(to_addr):
    from core.wallet import abi
    data = abi.encode_call("mint", [{"type": "address"}, {"type": "uint256"}, {"type": "uint256"}],
                           [to_addr, 1, 0])
    price = 42 * 10 ** 13
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=PINNED_A, amount_raw=price,
                               max_spend_usd=500.0, idempotency_key="m")
    tx = {"to": PINNED_A, "data": data, "value": price, "chainId": 4663, "nonce": 1, "gas": 300_000,
          "maxFeePerGas": 10 ** 8}
    return tx_guard.authorize(
        intent, tx, holder=HOLDER, gate=PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=1000.0),
        execution_context=None, simulate_fn=lambda **_: Deltas(ok=True, native_delta=-price, gas_used=1),
        price_fn=lambda c, a: 3000.0, rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda c, t: False, account_rpc=CodeRpc("0x"))


def test_a_mint_to_a_pinned_account_address_refuses(monkeypatch):
    """A mint to someone else is not a holder move, so no delta names it — the calldata does."""
    _pinned(monkeypatch)
    d = _mint_to(erc6551.account_address(4663, PINNED_A, 7))
    assert d.allowed is False and "a call to the pinned collection" in d.reason, d.reason
    assert _mint_to(HOLDER).allowed is True


def test_unpinned_nothing_is_computed(monkeypatch):
    pin(monkeypatch)
    assert tx_guard._collection_account_destination_refusal("robinhood", {"to": PINNED_A, "data": "0x"}, ()) is None
