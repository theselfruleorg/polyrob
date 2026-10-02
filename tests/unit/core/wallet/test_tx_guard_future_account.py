"""C5 (polyrob-desk handoff-core) — no asset of ANY kind into the account of a token not minted yet.

``accountOf(k)`` of a pinned collection exists as an address before token ``k`` is minted; whoever
mints ``k`` later owns everything sent there. The NFT half was W2 (``test_tx_guard_nft_nesting``);
this is every other asset: ETH, an ERC-20 transfer, a router's recipient. A minted ``k`` is an
ordinary account (a deposit to an NFT's owner), and a failed ``ownerOf`` read refuses.
"""
from core.wallet import abi, erc6551, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from tests.collection_pins import CODE, pin, profile

HOLDER = "0x1111111111111111111111111111111111111111"
PINNED = "0xC011000000000000000000000000000000000001"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ROUTER = "0x7777777777777777777777777777777777777777"
MINTED, UNMINTED = 3, 4242
OWNER = "0x5555555555555555555555555555555555555555"


def _acct(k, chain_id=4663):
    return erc6551.account_address(chain_id, PINNED, k)


class OwnerRpc:
    """``ownerOf(MINTED)`` = OWNER; any other id reverts (not minted). ``broken`` = RPC down."""

    def __init__(self, broken=False):
        self.broken, self.asked = broken, []

    def __call__(self, method, params):
        if method == "eth_getCode":
            return CODE
        if method == "eth_call" and params[0]["data"][:10] == abi.selector("ownerOf(uint256)"):
            k = int(params[0]["data"][10:], 16)
            self.asked.append(k)
            if self.broken:
                raise RuntimeError("rpc down")
            if k != MINTED:
                raise RuntimeError("execution reverted: ERC721NonexistentToken")
            return "0x" + abi.encode([{"type": "address"}], [OWNER]).hex()
        raise AssertionError(f"unexpected rpc {method}")


def _authorize(intent, tx, deltas, rpc):
    return tx_guard.authorize(
        intent, dict(tx, chainId=4663, nonce=1, gas=100_000, maxFeePerGas=10 ** 8), holder=HOLDER,
        gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0), execution_context=None,
        simulate_fn=lambda **_: deltas, price_fn=lambda c, a: 1.0 if a else 3000.0,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda c, t: False, account_rpc=rpc)


def _eth(to):
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=to, amount_raw=10 ** 15,
                               max_spend_usd=10.0, idempotency_key="e")
    return intent, {"to": to, "data": "0x", "value": 10 ** 15}, Deltas(ok=True, native_delta=-10 ** 15,
                                                                        gas_used=21_000)


def _erc20(to):
    """On base: the guard knows USDC's decimals there."""
    data = abi.encode_call("transfer", [{"type": "address"}, {"type": "uint256"}], [to, 250_000])
    intent = tx_guard.TxIntent(chain="base", token=USDC, to=to, amount_raw=250_000,
                               max_spend_usd=5.0, idempotency_key="t")
    return intent, {"to": USDC, "data": data, "value": 0}, Deltas(
        ok=True, token_deltas={USDC: -250_000}, gas_used=60_000,
        holder_transfers=((USDC.lower(), to.lower(), 250_000),))


def test_eth_to_the_account_of_an_unminted_token_refuses(monkeypatch):
    pin(monkeypatch, profile(PINNED))
    rpc = OwnerRpc()
    d = _authorize(*_eth(_acct(UNMINTED)), rpc)
    assert d.allowed is False and "not minted yet" in d.reason, d.reason
    assert rpc.asked == [UNMINTED]


def test_erc20_to_the_account_of_an_unminted_token_refuses(monkeypatch):
    pin(monkeypatch, profile(PINNED, chain_id=8453))
    d = _authorize(*_erc20(_acct(UNMINTED, 8453)), OwnerRpc())
    assert d.allowed is False and f"#{UNMINTED}" in d.reason, d.reason


def test_a_router_recipient_in_the_calldata_refuses(monkeypatch):
    """A swap pays the router; the recipient is only an ABI word of the calldata."""
    pin(monkeypatch, profile(PINNED))
    data = abi.encode_call("swap", [{"type": "address"}, {"type": "uint256"}],
                           [_acct(UNMINTED), 1])
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=ROUTER, amount_raw=10 ** 15,
                               max_spend_usd=10.0, idempotency_key="s")
    d = _authorize(intent, {"to": ROUTER, "data": data, "value": 10 ** 15},
                   Deltas(ok=True, native_delta=-10 ** 15, gas_used=90_000), OwnerRpc())
    assert d.allowed is False and "not minted yet" in d.reason, d.reason


def test_the_account_of_a_minted_token_is_an_ordinary_destination(monkeypatch):
    pin(monkeypatch, profile(PINNED))
    assert _authorize(*_eth(_acct(MINTED)), OwnerRpc()).allowed is True
    pin(monkeypatch, profile(PINNED, chain_id=8453))
    d = _authorize(*_erc20(_acct(MINTED, 8453)), OwnerRpc())
    assert d.allowed is True, d.reason


def test_a_failed_owner_read_refuses(monkeypatch):
    pin(monkeypatch, profile(PINNED))
    d = _authorize(*_eth(_acct(MINTED)), OwnerRpc(broken=True))
    assert d.allowed is False and "failed" in d.reason and "failing closed" in d.reason


def test_an_ordinary_destination_reads_nothing(monkeypatch):
    pin(monkeypatch, profile(PINNED))
    rpc = OwnerRpc(broken=True)
    assert _authorize(*_eth("0x9999999999999999999999999999999999999999"), rpc).allowed is True
    assert rpc.asked == []


def test_no_pinned_collection_reads_nothing(monkeypatch):
    pin(monkeypatch)
    rpc = OwnerRpc(broken=True)
    assert _authorize(*_eth(_acct(UNMINTED)), rpc).allowed is True
    assert rpc.asked == []


def test_the_inverse_index_is_the_registry_math():
    for k in (0, 1, MINTED, UNMINTED, 6551):
        assert erc6551.collection_account_token_id(4663, PINNED, 6551, _acct(k)) == k
    assert erc6551.collection_account_token_id(4663, PINNED, 6551, HOLDER) is None
    assert erc6551.collection_account_token_id(4663, PINNED, 10, _acct(11)) is None
