"""The `collectibles` status section — what the agent HOLDS and what it MOVED.

Follows the `creations` precedent (042b): derive from the spend ledger every
verb already writes, rather than adding a second store that can disagree with
the chain. That disagreement is the shape behind the 2026-08-25 incident where
the agent published "book flat" to X while holding three positions.

⚠️ THE HONEST LIMIT, STATED IN THE SECTION ITSELF. A `wallet_spend` row exists
only for a SPEND, so an airdropped NFT has no row and telemetry alone cannot
answer "what do I hold". The section therefore reports two LABELLED things and
never conflates them:

  held  — the live enumeration read (or its `unavailable (<reason>)`)
  moved — the NFT moves the guard authorized, from telemetry

An empty `held` with no provider configured must never render as "you own
nothing". That is the whole rule this file exists to pin.
"""
import json
import os
import sqlite3

import pytest

from core.status_snapshot import STATE_OK, STATE_UNAVAILABLE, _collectibles_section


def _db(tmp_path, rows):
    path = os.path.join(str(tmp_path), "telemetry_events.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE telemetry_events (id INTEGER PRIMARY KEY, ts REAL, "
                "kind TEXT, user_id TEXT, session_id TEXT, source TEXT, attrs TEXT)")
    for ts, kind, uid, attrs in rows:
        con.execute("INSERT INTO telemetry_events (ts, kind, user_id, session_id, "
                    "source, attrs) VALUES (?,?,?,'','wallet',?)",
                    (ts, kind, uid, attrs))
    con.commit()
    con.close()
    return path


def _move(asset, to="0x" + "22" * 20, chain="base", tx="0xabc"):
    return json.dumps({"venue": "defi", "action": "nft_transfer",
                       "counterparty": to, "amount_usd": 0.02,
                       "result_ref": tx, "chain": chain, "asset": asset})


def test_an_absent_telemetry_store_is_reported_not_treated_as_empty(tmp_path):
    from core.status_snapshot import _guarded
    sec = _guarded("collectibles", _collectibles_section, "rob", str(tmp_path))
    assert sec.state == STATE_UNAVAILABLE
    assert sec.reason


def test_moves_are_derived_from_the_spend_ledger(tmp_path):
    _db(tmp_path, [(1.0, "wallet_spend", "rob",
                    _move("erc721:0x" + "44" * 20 + ":42"))])
    sec = _collectibles_section("rob", str(tmp_path))
    assert sec.state == STATE_OK
    assert sec.data["moved"], "the authorized move was not derived"
    assert sec.data["moved"][0]["asset"].endswith(":42")
    assert "42" in " ".join(sec.lines)


def test_a_spend_that_is_not_an_nft_move_is_ignored(tmp_path):
    _db(tmp_path, [(1.0, "wallet_spend", "rob",
                    json.dumps({"venue": "defi", "action": "swap",
                                "counterparty": "0xpool", "amount_usd": 5.0}))])
    sec = _collectibles_section("rob", str(tmp_path))
    assert sec.data["moved"] == []


def test_another_tenants_move_is_invisible(tmp_path):
    _db(tmp_path, [(1.0, "wallet_spend", "someone_else",
                    _move("erc721:0xaa:1"))])
    sec = _collectibles_section("rob", str(tmp_path))
    assert sec.data["moved"] == []


def test_an_unparseable_row_is_counted_and_surfaced_never_dropped(tmp_path):
    _db(tmp_path, [(1.0, "wallet_spend", "rob", "{ not json")])
    sec = _collectibles_section("rob", str(tmp_path))
    assert sec.data["unreadable_rows"] == 1
    assert "incomplete" in " ".join(sec.lines).lower()


# --- the held half: unavailable is NOT empty -------------------------------

def test_with_no_enumeration_provider_held_says_so(tmp_path, monkeypatch):
    """⚠️ The load-bearing case. 'You hold nothing' and 'I could not look' are
    different facts, and only one of them is reassuring."""
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    _db(tmp_path, [])
    sec = _collectibles_section("rob", str(tmp_path))
    assert sec.data["held"] is None, "an unavailable read must not become []"
    blob = " ".join(sec.lines).lower()
    assert "not read" in blob or "could not" in blob or "unavailable" in blob
    # …and emphatically NOT a claim about what is owned.
    assert "hold nothing" not in blob
    assert "held: none" not in blob


def test_the_snapshot_never_makes_a_network_read_for_holdings(tmp_path, monkeypatch):
    """Status is cheap by default — the same contract `include_balances` carries
    for the ledger. Asserted at the REAL entry point: `build_status_snapshot`
    must not hand the section an enumerator, or every /status on every seat
    would hit an indexer."""
    monkeypatch.setenv("ALCHEMY_API_KEY", "k")
    import inspect

    import core.status_snapshot as mod
    src = inspect.getsource(mod._build_core)
    assert "enumerate_fn" not in src, (
        "the snapshot passes an enumerator, so /status now hits the network")
    _db(tmp_path, [])
    sec = _collectibles_section("rob", str(tmp_path))
    assert sec.data["held"] is None


def test_held_is_read_when_explicitly_asked_for(tmp_path, monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "k")
    _db(tmp_path, [])
    held = [{"contract": "0x" + "44" * 20, "token_id": "42",
             "standard": "erc721", "name": "Test", "balance": "1"}]
    sec = _collectibles_section("rob", str(tmp_path),
                                enumerate_fn=lambda **kw: held)
    assert sec.data["held"] == held
    assert "Test" in " ".join(sec.lines)


def test_a_failed_held_read_renders_its_reason(tmp_path):
    _db(tmp_path, [])

    def _fail(**kw):
        raise RuntimeError("indexer 503")

    sec = _collectibles_section("rob", str(tmp_path), enumerate_fn=_fail)
    assert sec.data["held"] is None
    assert "503" in " ".join(sec.lines)


def test_a_genuinely_empty_read_is_distinguished_from_an_unavailable_one(tmp_path):
    _db(tmp_path, [])
    sec = _collectibles_section("rob", str(tmp_path), enumerate_fn=lambda **kw: [])
    assert sec.data["held"] == []
    blob = " ".join(sec.lines).lower()
    assert "none" in blob or "no nft" in blob or "nothing" in blob


# --- wiring ---------------------------------------------------------------

def test_collectibles_is_in_the_section_order_and_titled():
    from core.status_snapshot import SECTION_ORDER
    from core.status_render import _SECTION_TITLES
    assert "collectibles" in SECTION_ORDER
    assert _SECTION_TITLES.get("collectibles")


def test_the_snapshot_always_carries_the_section(tmp_path):
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("rob", data_dir=str(tmp_path))
    assert "collectibles" in snap.sections
