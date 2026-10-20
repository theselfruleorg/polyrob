"""085 M-2: ``polyrob wallet asset add --verify`` warns when a contract calls
itself a stablecoin but its address is not a pinned stable."""
from core.payments.assets import unpinned_stable_note

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
FAKE = "0x" + "ab" * 20


def test_a_stable_symbol_at_an_unpinned_address_warns():
    out = unpinned_stable_note("base", FAKE, "USDC")
    assert out.startswith("\n") and "not a pinned stablecoin" in out
    assert "NOT priced at $1" in out


def test_the_pinned_usdc_does_not_warn():
    assert unpinned_stable_note("base", BASE_USDC.lower(), "USDC") == ""


def test_an_ordinary_token_does_not_warn():
    assert unpinned_stable_note("base", FAKE, "ROB") == ""


def test_the_cli_add_wires_the_note():
    import inspect
    from cli.commands import wallet
    assert "unpinned_stable_note(chain, address, symbol)" in inspect.getsource(wallet)
