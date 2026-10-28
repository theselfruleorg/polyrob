"""086 W0 §3.10: ``core.position_retire.retire_row`` closes a book row ONLY on
a measured zero or dust balance, and records the measurement as proof.

The 0.16-token ROBIN row blocked a goal for 10 days: /writeoff is a LOSS
verdict, so a row the chain no longer backs had no honest way to close.
"""
import json

import pytest

from core import open_positions as op
from core.open_positions import PositionDelta, apply_delta
from core.position_retire import retire_proof, retire_row

MEME = "0xb200000000000000000000ea8625786a776539fb"


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "open_positions.db")
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 0.16, 3.0), db_path=path)
    return path


def _retire(db, **kw):
    args = dict(account="", measured_qty=0.0, measured_usd=0.0,
                proof="balanceOf read: 0 raw", db_path=db, now=1000.0)
    args.update(kw)
    return retire_row("u1", "base", MEME, **args)


def test_a_zero_balance_retires_the_row_with_proof(db):
    out = _retire(db)
    assert out.retired, out.reason
    pos = op.get_position("u1", "base", MEME, db_path=db)
    assert pos.status == op.STATUS_RETIRED and pos.entry_usd == 3.0  # history kept
    proof = retire_proof("u1", "base", MEME, db_path=db)
    assert proof["measured_qty"] == 0.0 and proof["proof"] == "balanceOf read: 0 raw"
    assert proof["ts"] == 1000.0 and proof["dust_usd"] == op.DUST_USD
    assert op.positions_for("u1", db_path=db) == []


def test_a_dust_balance_retires(db):
    assert _retire(db, measured_qty=0.01, measured_usd=0.40).retired


@pytest.mark.parametrize("qty,usd", [(0.16, 1.0), (0.16, 5.0), (0.16, None),
                                     (None, 0.0), (-1.0, 0.0), (float("nan"), 0.0),
                                     (0.16, float("nan"))])
def test_anything_but_a_measured_zero_or_dust_is_refused(db, qty, usd):
    out = _retire(db, measured_qty=qty, measured_usd=usd)
    assert not out.retired and out.reason
    assert op.get_position("u1", "base", MEME, db_path=db).status == op.STATUS_OPEN


def test_no_proof_is_refused(db):
    assert not _retire(db, proof="  ").retired


def test_an_untracked_or_already_retired_row_is_refused(db):
    assert not retire_row("u1", "base", "0x" + "1" * 40, account="", measured_qty=0.0,
                          measured_usd=0.0, proof="p", db_path=db).retired
    assert _retire(db).retired
    again = _retire(db)
    assert not again.retired and "already retired" in again.reason


def test_another_tenant_cannot_retire_my_row(db):
    out = retire_row("intruder", "base", MEME, account="", measured_qty=0.0,
                     measured_usd=0.0, proof="p", db_path=db)
    assert not out.retired
    assert op.get_position("u1", "base", MEME, db_path=db).status == op.STATUS_OPEN


def test_a_missing_store_is_never_created(tmp_path):
    path = str(tmp_path / "absent.db")
    assert not retire_row("u1", "base", MEME, account="", measured_qty=0.0,
                          measured_usd=0.0, proof="p", db_path=path).retired
    import os
    assert not os.path.exists(path)


def test_the_proof_column_is_json(db):
    _retire(db)
    from core.sqlite_util import execute_retry
    row = execute_retry(db, "SELECT status_proof, status_reason FROM open_positions",
                        (), fetch="one")
    assert json.loads(row["status_proof"])["proof"] == "balanceOf read: 0 raw"
    assert row["status_reason"].startswith("retired")
