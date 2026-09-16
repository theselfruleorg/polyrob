"""One explorer-link helper (043 A38 / A5).

Before this existed, every address and hash an owner saw across `/status`,
`/wallet`, and the tx-execution notices was bare text — no link to verify it
against. `explorer_url` is the ONE place that knows the path shape (EVM
`/address|/tx|/token`, Solana `/account|/tx|/token`) so no caller guesses one.

`None`, never a broken link, on an unknown chain, a chain with no explorer
pinned, an unknown `kind`, or an empty `ref` — a helper that fabricates a URL
is worse than a caller that renders no link at all.
"""
import pytest

from core.wallet.chains import explorer_url


def test_explorer_url_shapes():
    assert explorer_url("base", "tx", "0xabc") == "https://basescan.org/tx/0xabc"
    assert explorer_url("solana", "address", "2RLN") == "https://solscan.io/account/2RLN"
    assert explorer_url("nochain", "tx", "0x") is None


def test_evm_address_and_token_paths():
    assert explorer_url("ethereum", "address", "0xdead") == "https://etherscan.io/address/0xdead"
    assert explorer_url("base", "token", "0xTOKEN") == "https://basescan.org/token/0xTOKEN"
    assert explorer_url("arbitrum", "tx", "0xabc") == "https://arbiscan.io/tx/0xabc"
    assert explorer_url("polygon", "address", "0xdead") == "https://polygonscan.com/address/0xdead"


def test_solana_tx_and_token_paths():
    assert explorer_url("solana", "tx", "5abc") == "https://solscan.io/tx/5abc"
    assert explorer_url("solana", "token", "MintAddr") == "https://solscan.io/token/MintAddr"


def test_robinhood_uses_the_canonical_measured_host():
    """The canonical host, never the redirector.

    Measured 2026-09-15: `https://explorer.mainnet.chain.robinhood.com/`
    answers 301 to `https://robinhoodchain.blockscout.com/`, and the
    canonical host answers 200 on `/address/<addr>` and `/token/<addr>`.
    Pinned so the redirector cannot creep back in as a guess.
    """
    addr = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
    assert explorer_url("robinhood", "address", addr) == (
        f"https://robinhoodchain.blockscout.com/address/{addr}")
    assert explorer_url("robinhood", "token", addr) == (
        f"https://robinhoodchain.blockscout.com/token/{addr}")
    assert explorer_url("robinhood", "tx", "0xabc") == (
        "https://robinhoodchain.blockscout.com/tx/0xabc")


def test_unknown_chain_is_none():
    assert explorer_url("nochain", "address", "0xdead") is None


def test_empty_ref_is_none():
    assert explorer_url("base", "tx", "") is None
    assert explorer_url("base", "tx", None) is None


def test_unknown_kind_is_none():
    assert explorer_url("base", "swap", "0xabc") is None


def test_never_raises_on_garbage_input():
    assert explorer_url(None, "tx", "0xabc") is None
    assert explorer_url(123, "tx", "0xabc") is None
