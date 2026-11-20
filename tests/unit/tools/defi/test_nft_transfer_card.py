"""124 D2: the owner's card for an NFT send says what the NFT's account holds."""
from core.wallet import erc6551
from tools.controller.grant_card import card_context_lines
from tools.defi.nft_verbs import nft_transfer_card_context

NFT = "0x4444444444444444444444444444444444444444"
ACCOUNT = erc6551.account_address(4663, NFT, 7, implementation=erc6551.ACCOUNT_V3_IMPL).lower()
PARAMS = {"chain": "robinhood", "contract": NFT, "token_id": 7, "to": "0x" + "99" * 20}


def _rpc(*, deployed=True, broken=False):
    def rpc(method, params):
        if broken:
            raise RuntimeError("rpc down")
        if method == "eth_call":
            return "0x"                      # no accountImplementation()
        if method == "eth_getCode":
            return "0x6001" if deployed and params[0].lower() == ACCOUNT else "0x"
        if method == "eth_getBalance":
            return hex(5 * 10 ** 17)
        raise AssertionError(method)
    return rpc


def test_a_deployed_account_is_named_with_its_native_balance():
    lines = nft_transfer_card_context(PARAMS, rpc=_rpc())
    assert len(lines) == 1 and ACCOUNT in lines[0] and "0.500000" in lines[0]
    assert "leaves with the NFT" in lines[0]


def test_no_account_is_said():
    assert "no deployed ERC-6551 account" in nft_transfer_card_context(PARAMS, rpc=_rpc(deployed=False))[0]


def test_an_unreadable_account_is_said_on_the_card():
    assert "UNREADABLE" in nft_transfer_card_context(PARAMS, rpc=_rpc(broken=True))[0]


def test_an_erc1155_has_no_account_line():
    assert nft_transfer_card_context(dict(PARAMS, standard="erc1155"), rpc=_rpc()) == []


def test_the_card_hook_is_registered():
    import tools.defi.call_verb  # noqa: F401 — registers the hooks
    lines = card_context_lines("defi_trade_nft_transfer", dict(PARAMS, standard="erc1155"))
    assert lines == []
