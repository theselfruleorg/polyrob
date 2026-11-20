"""124 W3: an unpinned collection is read from the chain (collection_registry.chain_profile)."""
import pytest

from core.wallet import collection_registry, erc6551, nft_account

NFT = "0x4444444444444444444444444444444444444444"
TREASURY = "0x1111111111111111111111111111111111111111"
ACCOUNT = erc6551.account_address(4663, NFT, 7, implementation=erc6551.ACCOUNT_V3_IMPL)
CLONE = ("0x363d3d373d3d3d363d73" + erc6551.ACCOUNT_V3_IMPL[2:].lower()
         + "5af43d82803e903d91602b57fd5bf3" + "00" * 128)


class Chain:
    def __init__(self, *, erc721=True, owner=TREASURY):
        self.erc721, self.owner = erc721, owner

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            return hex(0x100)
        if method == "eth_getBalance":
            return "0x0"
        if method == "eth_getCode":
            a = params[0].lower()
            if a == NFT.lower():
                return "0x6080604052"
            return CLONE if a == ACCOUNT.lower() else "0x"
        assert method == "eth_call", method
        data = params[0]["data"]
        if data.startswith("0x01ffc9a7"):
            return "0x" + f"{int(self.erc721):064x}"
        if data.startswith("0x6352211e"):
            return "0x" + "0" * 24 + self.owner[2:]
        raise RuntimeError("execution reverted")


@pytest.fixture(autouse=True)
def _no_pins(monkeypatch):
    collection_registry._CHAIN_PROFILES.clear()
    monkeypatch.setattr("core.wallet.collection_registry.profiles", lambda: ())


def test_an_unpinned_collection_is_read_from_the_chain():
    p = collection_registry.chain_profile("robinhood", NFT, rpc=Chain())
    assert p.pinned is False and p.address == NFT.lower()
    assert p.max_supply == collection_registry.UNBOUNDED_SUPPLY and p.capabilities == ()
    assert p.accounts[0].implementation == erc6551.ACCOUNT_V3_IMPL.lower()


def test_a_contract_that_is_no_erc721_is_named():
    with pytest.raises(ValueError, match="ERC-721"):
        collection_registry.chain_profile("robinhood", NFT, rpc=Chain(erc721=False))


def test_resolve_takes_an_unpinned_nft_the_treasury_owns():
    held = nft_account.resolve("robinhood", rpc=Chain(), treasury=TREASURY, nft=f"{NFT}#7")
    assert held.pinned is False and held.account.lower() == ACCOUNT.lower()


def test_resolve_still_needs_the_owner():
    with pytest.raises(nft_account.NftAccountError, match="does not own"):
        nft_account.resolve("robinhood", rpc=Chain(owner="0x" + "22" * 20), treasury=TREASURY,
                            nft=f"{NFT}#7")



class AnyChain(Chain):
    """Every address is an ERC-721 with code; counts the code reads (one per profile read)."""

    def __init__(self):
        super().__init__()
        self.reads = []

    def __call__(self, method, params):
        if method == "eth_getCode" and params[0].lower() != ACCOUNT.lower():
            self.reads.append(params[0].lower())
            return "0x6080604052"
        return super().__call__(method, params)


def test_the_chain_profile_cache_is_bounded_least_recently_used_first(monkeypatch):
    from core.wallet import collection_registry as R
    monkeypatch.setattr(R, "CHAIN_PROFILE_MAX", 2)
    rpc = AnyChain()
    a, b, c = ("0x" + f"{i:040x}" for i in (0xa1, 0xb2, 0xc3))
    for addr in (a, b, a, c):                  # a is used again: b is the oldest when c comes
        R.chain_profile("robinhood", addr, rpc=rpc)
    assert [k[1] for k in R._CHAIN_PROFILES] == [a, c]
    seen = len(rpc.reads)
    R.chain_profile("robinhood", a, rpc=rpc)   # still cached: no read
    assert len(rpc.reads) == seen
    R.chain_profile("robinhood", b, rpc=rpc)   # b was dropped: read again
    assert len(rpc.reads) > seen and len(R._CHAIN_PROFILES) == 2
