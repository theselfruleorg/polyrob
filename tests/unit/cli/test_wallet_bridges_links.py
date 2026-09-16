"""043 D1 fix round 1 — `polyrob wallet bridges` links the RIGHT chain.

⚠️ The defect: `tx_ref` on a bridge row is the ORIGIN transaction
(`bridge_verb` settles `tx_ref=signature` on the send), and the listing resolved
it against `dest_chain_id`. On the one route v1 supports — Solana origin, EVM
destination — that produced a base58 Solana signature inside an EVM explorer
URL: a link that looks authoritative and resolves to nothing.

The rule is `core/wallet/tx_notify.py`'s: a link only for the chain the
transaction LANDED on, and no link at all when that chain cannot be named.
"""
import pytest
from click.testing import CliRunner

from cli.commands.wallet import _chain_name, _tx_link
from core.wallet.bridge_guard import chain_name_for_id
from tools.defi.providers.relay_bridge import SOLANA_CHAIN_ID


# --- the ONE resolver -------------------------------------------------------- #

def test_a_falsey_chain_id_is_not_solana():
    """Solana's registry row carries `chain_id=0` because EIP-155 has no Solana
    analogue — and 0 is also what a missing column and `int(None or 0)` read as.
    A scan that matched it would resolve "I do not know" to a real chain."""
    assert chain_name_for_id(0) is None
    assert chain_name_for_id(None) is None
    assert chain_name_for_id("") is None
    assert chain_name_for_id("nonsense") is None


def test_the_core_resolver_knows_registry_ids_only():
    """⚠️ Relay's pseudo id is a PROVIDER identifier, not a chain one. `core`
    may not import `tools` (tests/test_layering_ratchet.py), and a copy of the
    constant in core would be a second place for it to drift — so the core
    resolver does not claim to know it."""
    assert chain_name_for_id(SOLANA_CHAIN_ID) is None


def test_the_seat_resolves_the_id_the_bridge_actually_stores():
    """`record_pending` writes Relay's own pseudo chain id for an SVM origin,
    and the seat that renders the row is where that is resolved."""
    from cli.commands.wallet import _bridge_chain_key
    assert _bridge_chain_key(SOLANA_CHAIN_ID) == "solana"
    assert _bridge_chain_key(8453) == "base"
    assert _bridge_chain_key(0) is None
    assert _bridge_chain_key(None) is None


@pytest.mark.parametrize("chain_id,name", [(8453, "base"), (4663, "robinhood"),
                                           (1, "ethereum")])
def test_a_pinned_evm_chain_still_resolves(chain_id, name):
    assert chain_name_for_id(chain_id) == name


def test_an_unknown_chain_stays_unknown():
    assert chain_name_for_id(999999) is None


# --- the link ---------------------------------------------------------------- #

def test_a_solana_signature_links_to_a_solana_explorer():
    link = _tx_link(SOLANA_CHAIN_ID, "5Kd3NbBQqDzBmJ8x")
    assert link.startswith("https://solscan.io/tx/")
    assert link.endswith("5Kd3NbBQqDzBmJ8x")


def test_an_evm_hash_links_to_that_chains_explorer():
    assert _tx_link(8453, "0xabc") == "https://basescan.org/tx/0xabc"


def test_no_link_at_all_when_the_chain_cannot_be_named():
    """A helper that renders a broken link is worse than a caller that omits
    the line."""
    assert _tx_link(999999, "0xabc") == ""
    assert _tx_link(None, "0xabc") == ""
    assert _tx_link(0, "5Kd3Nb") == ""


def test_no_link_without_a_transaction():
    assert _tx_link(8453, None) == ""
    assert _tx_link(8453, "") == ""


def test_a_chain_with_no_id_still_names_itself_honestly():
    assert _chain_name(999999) == "chain 999999"
    assert _chain_name(8453) == "base (chain 8453)"


# --- the listing ------------------------------------------------------------- #

def _row(**kw):
    base = {
        "id": "b1", "user_id": "u1", "request_id": "req-1",
        "origin_chain_id": SOLANA_CHAIN_ID, "dest_chain_id": 4663,
        "recipient": "0xabc", "currency_out": "native",
        "amount_in_raw": "1", "min_out_raw": "1", "amount_usd": 12.5,
        "state": "in_flight", "tx_ref": "5Kd3NbBQqDzBmJ8x",
        "balance_before": "0", "created_at": 1_700_000_000.0,
        "escalated_at": None, "detail": "",
    }
    base.update(kw)
    return base


def _run(monkeypatch, rows):
    from cli.commands import wallet as mod
    from core.wallet import bridge_guard
    monkeypatch.setattr(bridge_guard, "open_bridges", lambda uid: rows)
    monkeypatch.setattr("core.identity.resolve_identity", lambda: "u1")
    return CliRunner().invoke(mod.wallet_bridges, [])


def test_the_listing_links_the_origin_never_the_destination(monkeypatch):
    result = _run(monkeypatch, [_row()])
    assert result.exit_code == 0, result.output
    assert "solscan.io/tx/5Kd3NbBQqDzBmJ8x" in result.output
    assert "basescan" not in result.output
    # …and the destination is still NAMED, because a chain id is not a place.
    assert "robinhood (chain 4663)" in result.output


def test_a_row_with_no_origin_chain_prints_the_hash_without_a_link(monkeypatch):
    result = _run(monkeypatch, [_row(origin_chain_id=None)])
    assert result.exit_code == 0, result.output
    assert "http" not in result.output
    assert "tx 5Kd3NbBQqDzBmJ8x" in result.output


def test_an_unreadable_store_is_unknown_not_all_clear(monkeypatch):
    from cli.commands import wallet as mod
    from core.wallet import bridge_guard

    def boom(uid):
        raise OSError("disk")

    monkeypatch.setattr(bridge_guard, "open_bridges", boom)
    monkeypatch.setattr("core.identity.resolve_identity", lambda: "u1")
    result = CliRunner().invoke(mod.wallet_bridges, [])
    assert result.exit_code != 0
    assert "UNKNOWN" in result.output


# --- the row carries what the link needs ------------------------------------- #

def test_an_open_row_carries_its_origin_chain(tmp_path):
    """Without `origin_chain_id` in the open-row read there is no honest link to
    build, which is how the wrong one got built."""
    import types

    from core.wallet import bridge_guard as bg

    db = str(tmp_path / "bridges.db")
    quote = types.SimpleNamespace(
        request_id="req-1", origin_chain_id=SOLANA_CHAIN_ID, dest_chain_id=4663,
        recipient="0xabc", currency_out="native", amount_in_raw=10, min_out_raw=9)
    bg.record_pending(user_id="u1", quote=quote, amount_usd=1.0,
                      balance_before=0, db_path=db)
    rows = bg.open_bridges("u1", db_path=db)
    assert len(rows) == 1
    assert int(rows[0]["origin_chain_id"]) == SOLANA_CHAIN_ID
