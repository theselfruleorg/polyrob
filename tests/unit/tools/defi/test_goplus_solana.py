"""GoPlus on Solana — a DIFFERENT endpoint and a different risk taxonomy.

The EVM screen keys on is_honeypot / buy_tax / sell_tax. Solana has none of
those: its drain vectors are mint, freeze and close AUTHORITIES, mutable
metadata, and upgradable transfer hooks. Reusing the EVM parser would have
returned `available=True` with an EMPTY check list on every Solana token — which
reads as "screened, nothing found" and is strictly worse than "unavailable".
That is the failure these tests exist to prevent.
"""
from tools.defi.providers import goplus


def _sol(**over):
    data = {
        "mintable": {"authority": [], "status": "0"},
        "freezable": {"authority": [], "status": "0"},
        "closable": {"authority": [], "status": "0"},
        "metadata_mutable": {"metadata_upgrade_authority": [], "status": "0"},
        "balance_mutable_authority": {"authority": [], "status": "0"},
        "default_account_state_upgradable": {"authority": [], "status": "0"},
        "transfer_fee_upgradable": {"authority": [], "status": "0"},
        "transfer_hook_upgradable": {"authority": [], "status": "0"},
        "non_transferable": "0",
    }
    data.update(over)
    return {"code": 1, "result": {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": data}}


def test_a_clean_solana_token_enumerates_its_checks():
    v = goplus.parse_solana_screen(_sol())
    assert v.available is True
    assert v.checks, "an empty check list would read as 'screened, nothing found'"
    assert "mintable" in v.checks and "freezable" in v.checks
    assert v.flags == []


def test_a_live_mint_authority_raises_a_flag():
    v = goplus.parse_solana_screen(_sol(
        mintable={"authority": [{"address": "x"}], "status": "1"}))
    assert "mintable" in v.flags


def test_a_freeze_authority_raises_a_flag():
    """Solana's closest thing to a honeypot: an account that can be frozen
    cannot be sold, and no EVM check would catch it."""
    v = goplus.parse_solana_screen(_sol(
        freezable={"authority": [{"address": "x"}], "status": "1"}))
    assert "freezable" in v.flags


def test_a_close_authority_raises_a_flag():
    """CloseAccount has no EVM analogue at all — it is a drain primitive."""
    v = goplus.parse_solana_screen(_sol(
        closable={"authority": [{"address": "x"}], "status": "1"}))
    assert "closable" in v.flags


def test_a_plain_string_field_is_read_too():
    v = goplus.parse_solana_screen(_sol(non_transferable="1"))
    assert "non_transferable" in v.flags


def test_an_empty_or_error_payload_is_unavailable_not_clean():
    for payload in (None, {}, {"code": 0}, {"code": 1, "result": {}}):
        assert goplus.parse_solana_screen(payload).available is False


def test_a_payload_with_no_recognised_field_is_unavailable():
    """A schema change must surface as UNAVAILABLE, never as a clean pass."""
    payload = {"code": 1, "result": {"addr": {"totally": "different"}}}
    assert goplus.parse_solana_screen(payload).available is False


def test_the_evm_parser_is_not_used_for_solana_shapes():
    """The regression this file exists for."""
    v = goplus.parse_screen(_sol())
    assert v.checks == {} or v.available is False


def test_solana_uses_its_own_endpoint_path():
    assert goplus.api_url("solana").endswith("/solana/token_security")
    assert goplus.api_url("base").endswith("/token_security/8453")


def test_an_uncovered_chain_has_no_url(monkeypatch):
    """A row with no goplus_id yields no URL, so `screen` reports UNAVAILABLE
    rather than an unscreened pass. Property-based: every chain in the registry
    carries an id now, so naming one would rot."""
    import dataclasses
    from core.wallet import chains
    bare = dataclasses.replace(chains.get("base"), goplus_id=None)
    monkeypatch.setattr(chains, "get", lambda name: bare if name == "base" else None)
    assert goplus.api_url("base") is None


def test_an_unknown_chain_has_no_url():
    assert goplus.api_url("nosuchchain") is None
