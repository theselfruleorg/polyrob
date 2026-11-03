"""098 MO-16: the store-full verdict is per tenant and read from the durable
verdict store — never a per-process cache."""
import time
from types import SimpleNamespace

import pytest

import modules.memory.registry as R


@pytest.fixture
def store(monkeypatch):
    data, calls = {}, []
    import core.credential_verdicts as cv

    def record(kind, key="", **kw):
        calls.append(("record", key))
        data[(kind, key)] = SimpleNamespace(last_seen=time.time())

    def clear(kind, key=""):
        calls.append(("clear", key))
        data.pop((kind, key), None)
    monkeypatch.setattr(cv, "record_rejection", record)
    monkeypatch.setattr(cv, "clear_rejection", clear)
    monkeypatch.setattr(cv, "verdict", lambda kind, key="": data.get((kind, key)))
    return data, calls


def test_two_registries_share_verdict_state(store):
    data, calls = store
    # "process A" clears (no verdict yet), "process B" records full, A clears it:
    R._note_store_ok("u1")
    R._note_store_full("u1")
    assert (R.STORE_FULL_KIND, R.store_full_key("u1")) in data
    R._note_store_ok("u1")          # A sees B's verdict through the durable store
    assert data == {}
    R._note_store_full("u1")        # a new fill after the clear is told at once
    assert [c for c in calls if c[0] == "record"] == [("record", "cross_session:u1")] * 2


def test_other_tenant_insert_does_not_clear_full(store):
    data, calls = store
    R._note_store_full("x")
    R._note_store_ok("y")
    assert (R.STORE_FULL_KIND, R.store_full_key("x")) in data
    assert ("clear", "cross_session:x") not in calls


def test_repeat_full_is_rate_limited(store):
    data, calls = store
    R._note_store_full("u1")
    R._note_store_full("u1")
    assert len([c for c in calls if c[0] == "record"]) == 1
