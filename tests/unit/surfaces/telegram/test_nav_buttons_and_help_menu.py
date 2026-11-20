"""2026-11-20 Telegram UX review, W2 + H1/H2: a button that runs a whole command line
(``core.surfaces.nav``) and the short `/help` menu built on it."""
import os

import pytest

from core.surfaces import nav
from core.surfaces.actions import is_action_command
from core.surfaces.command_reply import reply_actions, reply_text
from tests.unit.surfaces.telegram.test_unknown_command import _Agent, _cmd


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    s = nav.NavStore(os.path.join(str(tmp_path), "nav.db"))
    nav.set_store(s)
    yield s
    nav.set_store(None)


def test_a_line_with_arguments_rides_a_stored_token():
    [a] = nav.actions("alice", [nav.NavButton("Reset", "/model reset")])
    assert a.command.startswith("/nav_") and is_action_command(a.command)
    assert nav.resolve("alice", a.command) == ("/model reset", "")


def test_a_bare_verb_rides_the_button_as_typed():
    [a] = nav.actions("alice", [nav.NavButton("Status", "/status")])
    assert a.command == "/status"


def test_the_same_line_keeps_its_token():
    one = nav.actions("alice", [nav.NavButton("A", "/help money")])[0].command
    two = nav.actions("alice", [nav.NavButton("B", "/help money")])[0].command
    assert one == two


def test_another_tenant_reads_nothing():
    [a] = nav.actions("alice", [nav.NavButton("Reset", "/model reset")])
    assert nav.resolve("mallory", a.command) == (None, nav.EXPIRED_TEXT)


def test_a_go_line_or_an_unknown_verb_is_refused():
    assert nav.actions("alice", [nav.NavButton("Pay", "/send 1 USDC 0xabc go"),
                                 nav.NavButton("Bad", "/nosuchverb x")]) == []


def test_an_expired_line_reads_nothing(store):
    nav_id = store.put("alice", "/model reset", now=0.0)
    assert nav.resolve("alice", f"/nav_{nav_id}") == (None, nav.EXPIRED_TEXT)


def test_an_unreadable_store_is_named_never_expired(monkeypatch):
    class _Broken:
        def get(self, *a, **kw):
            raise OSError("disk")
    monkeypatch.setattr(nav, "store", lambda: _Broken())
    assert nav.resolve("alice", "/nav_" + "a" * 12) == (None, nav.UNREADABLE_TEXT)


@pytest.mark.asyncio
async def test_help_is_a_short_menu_with_group_buttons(tmp_path):
    from surfaces.telegram.harness import _handle_command
    reply = await _handle_command(_Agent(str(tmp_path)), _cmd("/help", "/help"), spawn=None)
    text, buttons = reply_text(reply), reply_actions(reply)
    assert len(text) < 1500 and "/help all" in text
    labels = [b.label for b in buttons]
    assert "Money" in labels and "All commands" in labels


@pytest.mark.asyncio
async def test_help_with_a_group_name_shows_that_group(tmp_path):
    from surfaces.telegram.harness import _handle_command
    reply = await _handle_command(_Agent(str(tmp_path)), _cmd("/help", "/help money"),
                                  spawn=None)
    assert reply_text(reply).startswith("**Money**") and "/wallet" in reply_text(reply)
    reply = await _handle_command(_Agent(str(tmp_path)), _cmd("/help", "/help needs you"),
                                  spawn=None)
    assert "/inbox" in reply_text(reply)


@pytest.mark.asyncio
async def test_a_tap_runs_the_stored_line(tmp_path):
    from surfaces.telegram.harness import _handle_command
    [a] = nav.actions("alice", [nav.NavButton("Money", "/help money")])
    reply = await _handle_command(_Agent(str(tmp_path)), _cmd(a.command, a.command),
                                  spawn=None)
    assert reply_text(reply).startswith("**Money**")


@pytest.mark.asyncio
async def test_an_unknown_tap_says_it_expired(tmp_path):
    from surfaces.telegram.harness import _handle_command
    reply = await _handle_command(_Agent(str(tmp_path)),
                                  _cmd("/nav_0123456789ab", "/nav_0123456789ab"), spawn=None)
    assert reply == nav.EXPIRED_TEXT
