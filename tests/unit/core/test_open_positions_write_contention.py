"""A position-book write under write contention is retried, never dropped in
silence: ``database is locked`` on the write transaction retries the whole
read-and-write, and a final failure is a WARNING."""
import logging
import sqlite3

import pytest

import core.open_positions as op
import core.sqlite_util as su

TOKEN = "0x" + "77" * 20


class _Conn:
    """A real connection whose first ``fail`` BEGIN IMMEDIATEs say the db is locked."""

    def __init__(self, real, state):
        self._real, self._state = real, state

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name in ("_real", "_state"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)

    def execute(self, sql, *args):
        if sql == "BEGIN IMMEDIATE" and self._state["fail"] > 0:
            self._state["fail"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args)


@pytest.fixture
def locked(monkeypatch, tmp_path):
    state = {"fail": 0}
    real_connect = su.wal_connect
    monkeypatch.setattr(su, "wal_connect",
                        lambda path, *a, **k: _Conn(real_connect(path, *a, **k), state))
    monkeypatch.setattr(op, "_contention_pause", lambda: None)
    return state, str(tmp_path / "open_positions.db")


def _buy():
    return op.PositionDelta(chain="base", address=TOKEN, symbol="TKN", qty=5.0,
                            cost_usd=40.0, qty_source="simulation")


def test_a_buy_that_meets_a_locked_book_is_retried_and_lands(locked):
    state, path = locked
    op._init(path)
    state["fail"] = 3
    op.apply_delta("owner-1", _buy(), db_path=path)
    row = op.get_position("owner-1", "base", TOKEN, db_path=path)
    assert row is not None and row.qty == 5.0
    assert state["fail"] == 0


def test_a_book_that_stays_locked_is_a_warning(locked, caplog):
    state, path = locked
    op._init(path)
    state["fail"] = 10 ** 6
    with caplog.at_level(logging.DEBUG, logger="core.open_positions"):
        op.apply_delta("owner-1", _buy(), db_path=path)
    assert op.get_position("owner-1", "base", TOKEN, db_path=path) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records
               if r.name == "core.open_positions")
