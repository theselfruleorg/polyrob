"""The durable dapp-session envelope store (043 A37).

The store exists so ``dapp_status`` can answer honestly after a restart: what the
wallet was armed for, what it spent, and what it refused, when the in-process
``_bridges`` dict is gone. The tests pin the two properties that keep it safe —
a reconnect REPLACES the row wholesale (never merges an old budget's spend into a
new one), and a persisted row survives a fresh store instance on the same file.
"""
import pytest

from core.dapp_session_store import (
    DappSessionStore,
    default_dapp_session_store_path,
    get_dapp_session_store,
)


def _envelope(**over):
    env = {
        "chain": "base",
        "address": "0xabc",
        "max_spend_usd": 5.0,
        "session_budget_usd": 20.0,
        "allow_contracts": [],
        "approval_timeout_sec": 300.0,
        "revoked": False,
        "spent_usd": 0.0,
        "sent": [],
        "refused": [],
    }
    env.update(over)
    return env


@pytest.fixture()
def store(tmp_path):
    return DappSessionStore(str(tmp_path / "dapp_sessions.db"))


def test_a_saved_session_reads_back(store):
    store.save("sess-1", "u1", _envelope(spent_usd=3.0))
    row = store.get("sess-1")
    assert row is not None
    assert row.session_id == "sess-1" and row.user_id == "u1"
    assert row.envelope["spent_usd"] == 3.0
    assert row.envelope["chain"] == "base"


def test_a_session_survives_a_reload_on_the_same_file(tmp_path):
    """A fresh store instance on the same file — the after-restart shape."""
    path = str(tmp_path / "dapp_sessions.db")
    DappSessionStore(path).save("sess-2", "u1",
                                _envelope(spent_usd=7.5, sent=[{"tx": "0x01"}]))
    reopened = DappSessionStore(path)
    row = reopened.get("sess-2")
    assert row is not None
    assert row.envelope["spent_usd"] == 7.5
    assert row.envelope["sent"] == [{"tx": "0x01"}]


def test_a_reconnect_replaces_the_row_never_merges(store):
    """The whole safety property: a second connect on one session cannot carry
    the previous budget's spend forward."""
    store.save("sess-3", "u1", _envelope(spent_usd=18.0, session_budget_usd=20.0))
    # Reconnect: a NEW envelope with a fresh (zero) spend.
    store.save("sess-3", "u1", _envelope(spent_usd=0.0, session_budget_usd=50.0))
    row = store.get("sess-3")
    assert row.envelope["spent_usd"] == 0.0
    assert row.envelope["session_budget_usd"] == 50.0


def test_created_at_is_preserved_across_an_update(store):
    store.save("sess-4", "u1", _envelope())
    first = store.get("sess-4").created_at
    store.save("sess-4", "u1", _envelope(spent_usd=1.0))
    again = store.get("sess-4")
    assert again.created_at == first
    assert again.updated_at >= first


def test_get_is_tenant_scoped_when_a_user_is_given(store):
    store.save("sess-5", "u1", _envelope())
    assert store.get("sess-5", user_id="u1") is not None
    # A different tenant asking for the same session id gets nothing.
    assert store.get("sess-5", user_id="u2") is None
    # No user_id filter still finds it (single-user / local).
    assert store.get("sess-5") is not None


def test_mark_revoked_flips_the_row(store):
    store.save("sess-6", "u1", _envelope())
    store.mark_revoked("sess-6")
    assert store.get("sess-6").revoked is True


def test_list_for_tenant_is_scoped_and_newest_first(store):
    store.save("a", "u1", _envelope())
    store.save("b", "u1", _envelope())
    store.save("c", "u2", _envelope())
    got = store.list_for_tenant("u1")
    assert {r.session_id for r in got} == {"a", "b"}
    assert store.list_for_tenant("u2")[0].session_id == "c"


def test_a_missing_session_reads_none(store):
    assert store.get("nope") is None


def test_a_broken_store_fails_open_not_raises(tmp_path):
    """Init failure leaves the store unready; reads/writes are no-ops, never a
    raise — a broken store must not break a connect or a spend."""
    store = DappSessionStore(str(tmp_path / "dapp_sessions.db"))
    store._ready = False
    store.save("x", "u1", _envelope())   # no raise
    assert store.get("x") is None
    assert store.list_for_tenant("u1") == []


def test_the_default_path_is_the_data_home_axis():
    assert default_dapp_session_store_path().endswith("dapp_sessions.db")


def test_get_store_is_a_singleton_per_path(tmp_path):
    path = str(tmp_path / "dapp_sessions.db")
    assert get_dapp_session_store(path) is get_dapp_session_store(path)
