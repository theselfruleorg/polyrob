"""`/dapp` is a READ, and a read never CREATES a store.

`dapp_reply` called `get_dapp_session_store()` unguarded. That constructor runs
its DDL, so asking "what is my wallet connected to?" on a box that has never
armed a page MINTED `dapp_sessions.db` just to answer "nothing" — and every
later "does this file exist" check (the db manifest, a backup, an operator's
`ls`, this very guard) then read a store nobody asked for.

Same guard, same reason, as `cli/commands/wallet.py::_dapp_store`.
"""
import os

import pytest

from core.dapp_session_store import default_dapp_session_store_path
from surfaces.telegram.dapp_ops import dapp_reply


@pytest.fixture
def virgin(tmp_path, monkeypatch):
    """A data home with no dapp-session store — a fresh install."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    # The module memoizes stores per path; a previous test's instance must not
    # answer for this one.
    import core.dapp_session_store as store_mod
    monkeypatch.setattr(store_mod, "_INSTANCES", {})
    path = default_dapp_session_store_path()
    assert not os.path.exists(path), "the fixture is not virgin"
    return path


@pytest.mark.parametrize("args", [[], ["list"], ["revoke", "sess-1"]])
def test_no_verb_creates_the_store_on_a_virgin_data_dir(virgin, args):
    out = dapp_reply("rob", args)
    assert not os.path.exists(virgin), (
        f"a READ created {virgin} — 'nothing is connected' must not cost a "
        f"database")
    # …and the answer is honest about WHY there is nothing, not a bare zero.
    assert "no dapp-session record here at all" in out
    assert "DURABLE record" in out


def test_an_existing_store_is_still_read(virgin):
    """The guard must not turn a real store into "no record"."""
    from core.dapp_session_store import DappSessionStore

    DappSessionStore(virgin)          # the ONE place the file is created
    assert os.path.exists(virgin)
    out = dapp_reply("rob", ["list"])
    assert "no dapp-session record here at all" not in out
    assert "No web page has been armed with my wallet on this box." in out


def test_a_non_owner_is_refused_before_any_path_is_touched(virgin):
    assert dapp_reply(None, ["list"]) == "Only the owner can use /dapp."
    assert not os.path.exists(virgin)
