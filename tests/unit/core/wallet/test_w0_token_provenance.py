"""W0 (token-management evaluation): the ``own_launch`` trust source.

The real PNL is the instance's OWN Pons launch; the identity gate still called
it "unverified" because the only trust sources were the canonical list and a
CLI-only pin table. A token this instance launched or deployed now reads
``verified=True, source="own_launch"`` from the one resolver.
"""
import json
import os

import pytest

from core.wallet import token_provenance as tp
from core.wallet.tokens import get_token_identity

REAL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"
FACTORY = "0x1111111111111111111111111111111111111111"


def _no_rpc(method, params, chain):
    return None


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    tp._reset_for_tests()
    monkeypatch.setattr(tp, "_PROBES", {})
    yield
    tp._reset_for_tests()


@pytest.fixture
def audit(tmp_path, monkeypatch):
    path = tmp_path / "wallet_isolate" / "wallet" / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(*rows):
        with open(path, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    import core.wallet.trade_index as ti
    monkeypatch.setattr(ti, "audit_path", lambda data_dir=None: str(path))
    return write


def _row(action, cp, chain="robinhood", ref="0xabc"):
    return {"venue": "defi", "action": action, "counterparty": cp, "chain": chain,
            "result_ref": ref, "amount_usd": 1.0}


def test_a_recorded_own_launch_reads_verified_from_the_one_resolver(tmp_path):
    db = str(tmp_path / "defi_tokens.db")
    assert get_token_identity("robinhood", REAL, db_path=db, rpc=_no_rpc).verified is False
    assert tp.record_own_token("robinhood", REAL.lower(), kind="launchpad_launch",
                               evidence="tx 0x1")
    ident = get_token_identity("robinhood", REAL, db_path=db, rpc=_no_rpc)
    assert ident.verified is True and ident.source == "own_launch"
    # a look-alike is untouched
    assert get_token_identity("robinhood", FAKE, db_path=db, rpc=_no_rpc).verified is False


def test_a_read_never_creates_the_store():
    assert tp.own_token("robinhood", REAL) is None
    assert not os.path.exists(tp.provenance_db_path())


def test_backfill_records_a_landed_deploy(audit):
    audit(_row("deploy_token", REAL, chain="base"), _row("deploy_token", None, chain="base"))
    row = tp.own_token("base", REAL)
    assert row and row["kind"] == "deploy_token"


def test_backfill_needs_a_probe_for_a_launchpad_row(audit, monkeypatch):
    # A reverted launch records the FACTORY as its counterparty: an unconfirmed
    # launchpad row never becomes trust by itself.
    audit(_row("launchpad_launch", FACTORY), _row("launchpad_launch", REAL))
    assert tp.own_token("robinhood", REAL) is None
    tp._reset_for_tests()
    monkeypatch.setitem(tp._PROBES, "pons",
                        lambda chain, addr: "deployer" if addr.lower() == REAL.lower() else None)
    row = tp.own_token("robinhood", REAL)
    assert row and row["kind"] == "launchpad_launch"
    assert tp.own_token("robinhood", FACTORY) is None


def test_the_spend_path_probe_catches_an_off_rail_launch(monkeypatch):
    calls = []

    def probe(chain, addr):
        calls.append(addr)
        return "pons deployer 0xcAda" if addr.lower() == REAL.lower() else None
    monkeypatch.setitem(tp._PROBES, "pons", probe)
    assert tp.own_token("robinhood", REAL) is None          # no probe on a plain read
    assert tp.own_token("robinhood", REAL, probe=True)["kind"] == "onchain_probe"
    assert tp.own_token("robinhood", REAL) is not None       # recorded: a lookup now
    assert tp.own_token("robinhood", FAKE, probe=True) is None
    n = len(calls)
    assert tp.own_token("robinhood", FAKE, probe=True) is None  # negative is cached
    assert len(calls) == n


def test_a_failing_probe_is_no_evidence(monkeypatch):
    def boom(chain, addr):
        raise RuntimeError("rpc down")
    monkeypatch.setitem(tp._PROBES, "pons", boom)
    assert tp.own_token("robinhood", REAL, probe=True) is None


def test_an_unreadable_store_grants_nothing(tmp_path):
    db = tmp_path / "prov.db"
    db.write_bytes(b"not a database" * 100)
    assert tp.own_token("robinhood", REAL, db_path=str(db)) is None


def test_the_trust_stores_are_not_agent_writable():
    from pathlib import Path
    from core.security.secret_guard import is_credential_file
    assert is_credential_file(Path("/srv/.polyrob/wallet/token_provenance.db"))
    assert is_credential_file(Path("/srv/.polyrob/wallet/token_pins.db"))
