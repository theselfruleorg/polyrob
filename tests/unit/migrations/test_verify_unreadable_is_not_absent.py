"""The v1.12-v1.15 verify gates: an UNREADABLE table fails the gate (logged);
a table that does not exist (an empty PRAGMA answer) still passes."""
import importlib
import logging

import pytest

_MODULES = [
    "migrations.versions.v1_12_0_x402_claim_refund_columns",
    "migrations.versions.v1_13_0_billing_failure_states",
    "migrations.versions.v1_14_0_deposit_marks_block",
    "migrations.versions.v1_15_0_deposit_sweep_state",
]


class _Unreadable:
    async def fetch_all(self, sql, *a):
        raise RuntimeError("database disk image is malformed")

    async def fetch_one(self, sql, *a):
        raise RuntimeError("database disk image is malformed")


class _NoTable:
    async def fetch_all(self, sql, *a):
        return []

    async def fetch_one(self, sql, *a):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _MODULES)
async def test_an_unreadable_table_fails_verify(name, caplog):
    mod = importlib.import_module(name)
    with caplog.at_level(logging.ERROR):
        assert await mod.verify(_Unreadable(), None) is False
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _MODULES)
async def test_an_absent_table_still_passes(name):
    mod = importlib.import_module(name)
    assert await mod.verify(_NoTable(), None) is True
