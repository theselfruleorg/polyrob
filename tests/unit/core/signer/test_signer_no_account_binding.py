"""069 v4 — the signer keeps no account binding.

The simple model: an agent acts through an NFT's account only as the NFT's owner, which the
``via_account`` pre-flight in ``tx_guard`` checks (here as in the agent). So the signer has no
``account.*`` op, no ``account_bound`` refusal and no ``[account_binding]`` table; a store or a
``signer.toml`` written before the simple model still opens, and its old binding data stays.
"""
import sqlite3

import pytest

from core.signer import protocol
from tests.unit.core.signer.conftest import make_config
from tests.unit.core.signer.test_066_p2_signer_schemas import _ok, _refused, _x402

ACCOUNT = "0xe0f47c083b28c76124129786cbd02a4489b86ec4"


@pytest.mark.parametrize("op, body", [
    ("account.bind", {"operator": "0x" + "11" * 20, "account": ACCOUNT}),
    ("account.bindings", {}),
    ("account.release", {"operator": "0x" + "11" * 20, "account": ACCOUNT}),
    ("account.journal_sign", {"message": "agent account journal"}),
])
def test_the_account_ops_are_gone(rig, op, body):
    assert op not in protocol.OPS
    _refused(rig.call(op, body, uid=0), protocol.UNKNOWN_SHAPE)


def test_no_account_bound_code_and_typed_data_signs(rig):
    assert not hasattr(protocol, "ACCOUNT_BOUND")
    _ok(rig.call("x402.authorize", _x402(rig)))


@pytest.mark.parametrize("table", ["account_binding", "desk"])
def test_a_retired_signer_toml_table_loads_and_is_ignored(tmp_path, caplog, table):
    with caplog.at_level("WARNING", logger="core.signer.caps"):
        cfg = make_config(tmp_path, **{table: {"agent_home": "/var/lib/polyrob"}})
    assert f"[{table}]" in caplog.text and "retired" in caplog.text
    assert not hasattr(cfg, "binding_agent_home")
    from core.signer.caps import render_signer_toml
    assert "[account_binding]" not in render_signer_toml(cfg)


@pytest.mark.parametrize("legacy", ["account_bindings", "desk_bindings"])
def test_an_old_bindings_table_is_left_in_place(tmp_path, legacy):
    """Never dropped, never read: the store opens and the rows survive."""
    from core.signer.store import SignerStore
    SignerStore(str(tmp_path))
    db = sqlite3.connect(str(tmp_path / "signer.sqlite"))
    db.execute(f"CREATE TABLE {legacy} (operator TEXT NOT NULL, account TEXT NOT NULL, "
               "chain_id INTEGER, source TEXT NOT NULL, ts REAL NOT NULL, "
               "PRIMARY KEY (operator, account))")
    db.execute(f"INSERT INTO {legacy} VALUES (?,?,?,?,?)", ("0x" + "11" * 20, ACCOUNT, 4663, "uid:998", 1.0))
    db.commit()
    db.close()
    store = SignerStore(str(tmp_path))
    assert not hasattr(store, "account_bindings")
    rows = sqlite3.connect(str(tmp_path / "signer.sqlite")).execute(f"SELECT * FROM {legacy}").fetchall()
    assert len(rows) == 1


def test_a_fresh_store_creates_no_bindings_table(tmp_path):
    from core.signer.store import SignerStore
    SignerStore(str(tmp_path))
    tables = {r[0] for r in sqlite3.connect(str(tmp_path / "signer.sqlite")).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "account_bindings" not in tables and "desk_bindings" not in tables
