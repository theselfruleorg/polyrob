"""077 — `/restart` and `/update`: quote → card → confirm → one request."""
import asyncio
import os
from types import SimpleNamespace

import pytest

from core import lifecycle as lc
from core.surfaces import cards


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "unit")
    # supervisor() also needs this process's cgroup to be a `.service`; a pytest
    # under a login shell sits in a `.scope`, which made the suite host-dependent.
    monkeypatch.setattr(lc, "_in_service_cgroup", lambda *a, **k: True)
    d = tmp_path / "data"
    lc.record_boot(data_home=d)  # this live, supervised process is "the service"
    return str(d)


@pytest.fixture
def st(tmp_path):
    return cards.CardStore(str(tmp_path / "c.db"))


ORIGIN = {"surface": "telegram", "chat_id": "42", "user_id": "u1"}


def test_bare_restart_quotes_and_cards(home, st):
    from surfaces.telegram.lifecycle_ops import restart_reply
    text = restart_reply("u1", home, [], ORIGIN)
    assert "I finish my current work first" in text and text.rstrip().endswith("/restart go")
    shown, card = cards.quote_card("u1", "/restart", [], text, st=st)
    assert card is not None, "a bare /restart must become a card"
    assert card.confirm_line == "/restart go"
    assert lc.read_request(data_home=home) is None, "a quote asks for nothing"


def test_restart_now_cards_its_own_line(home, st):
    from surfaces.telegram.lifecycle_ops import restart_reply
    text = restart_reply("u1", home, ["now"], ORIGIN)
    _, card = cards.quote_card("u1", "/restart", ["now"], text, st=st)
    assert card.confirm_line == "/restart now go"


def test_confirm_writes_one_request_with_the_chat(home, st):
    from surfaces.telegram.lifecycle_ops import restart_reply
    _, card = cards.quote_card("u1", "/restart", [], restart_reply("u1", home, [], ORIGIN),
                               st=st)
    p = cards.press(card.card_id, "ok", "u1", st=st)
    assert p.run == "/restart go"
    out = restart_reply("u1", home, p.run.split()[1:], ORIGIN)
    assert out.startswith("OK — restarting once")
    req = lc.read_request(data_home=home)
    assert req.origin == ORIGIN and not req.force and req.kind == lc.KIND_RESTART
    assert restart_reply("u1", home, ["go"], ORIGIN).startswith("Already asked")
    again = cards.press(card.card_id, "ok", "u1", st=st)
    assert again.run is None, "a second tap runs nothing twice"


def test_restart_refuses_without_a_service(tmp_path, monkeypatch):
    from surfaces.telegram.lifecycle_ops import restart_reply
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    d = tmp_path / "d"
    assert "not running as a background service" in restart_reply("u1", str(d), ["go"], ORIGIN)
    lc.record_boot(data_home=d)  # a terminal process: no supervisor
    out = restart_reply("u1", str(d), ["go"], ORIGIN)
    assert "terminal" in out and lc.read_request(data_home=d) is None


def test_restart_rejects_unknown_words(home):
    from surfaces.telegram.lifecycle_ops import RESTART_USAGE, restart_reply
    assert restart_reply("u1", home, ["everything"], ORIGIN) == RESTART_USAGE


def _check(**info):
    async def _c(channel):
        base = {"current": "1.3.0", "latest": "1.4.0", "update_available": True,
                "method": "editable_git", "error": None}
        base.update(info)
        return base, ""
    return _c


def test_update_quotes_then_requests(home, st):
    from surfaces.telegram.lifecycle_ops import update_reply
    text = asyncio.run(update_reply("u1", home, [], ORIGIN, check=_check()))
    assert "Update 1.3.0 → 1.4.0" in text and text.rstrip().endswith("/update go")
    _, card = cards.quote_card("u1", "/update", [], text, st=st)
    assert card is not None and card.confirm_line == "/update go"
    out = asyncio.run(update_reply("u1", home, ["go"], ORIGIN, check=_check()))
    assert out.startswith("OK — updating to 1.4.0")
    req = lc.read_request(data_home=home)
    assert req.kind == lc.KIND_UPDATE and req.target_version == "1.4.0" and req.channel == "stable"


def test_update_pre_channel_line(home, st):
    from surfaces.telegram.lifecycle_ops import update_reply
    text = asyncio.run(update_reply("u1", home, ["pre"], ORIGIN, check=_check()))
    _, card = cards.quote_card("u1", "/update", ["pre"], text, st=st)
    assert card.confirm_line == "/update pre go"


def test_update_when_current(home):
    from surfaces.telegram.lifecycle_ops import update_reply
    out = asyncio.run(update_reply("u1", home, [], ORIGIN,
                                   check=_check(update_available=False, latest="1.3.0")))
    assert out.startswith("You are on the latest version")


def test_update_on_an_install_that_does_not_update_itself(home):
    from surfaces.telegram.lifecycle_ops import update_reply
    out = asyncio.run(update_reply("u1", home, ["go"], ORIGIN, check=_check(method="pip")))
    assert "does not update itself" in out and lc.read_request(data_home=home) is None


def test_update_check_failure_is_said(home):
    from surfaces.telegram.lifecycle_ops import update_reply

    async def _broken(channel):
        return None, "the update check did not answer within 60 s"

    out = asyncio.run(update_reply("u1", home, [], ORIGIN, check=_broken))
    assert out.startswith("I could not check for an update")


def test_origin_of_reads_the_inbound_source():
    from surfaces.telegram.lifecycle_ops import origin_of
    src = SimpleNamespace(surface_id="telegram", chat_id=42, chat_type="private")
    result = SimpleNamespace(inbound=SimpleNamespace(identity=SimpleNamespace(source=src)))
    assert origin_of(result, "u1") == {"user_id": "u1", "surface": "telegram",
                                       "chat_id": "42", "chat_type": "private"}


def test_both_verbs_are_owner_admin_and_room_refused():
    from surfaces.telegram import admin_verbs, harness
    for verb in ("/restart", "/update"):
        assert verb in harness._OWNER_ADMIN_COMMANDS
        assert verb in harness._ROOM_REFUSED_COMMANDS
        assert admin_verbs.handler_for(verb) is not None


def test_a_bare_money_verb_is_still_never_carded(st):
    assert cards.quote_card("u1", "/send", [], "To send: /send go", st=st)[1] is None


def test_the_agent_may_propose_a_bare_restart(st):
    card = cards.proposal_card("u1", "/restart", [], why="a new MCP server", st=st)
    assert card.refresh_line == "/restart"
    with pytest.raises(ValueError):
        cards.proposal_card("u1", "/restart", ["go"], st=st)
    with pytest.raises(ValueError):
        cards.proposal_card("u1", "/send", [], st=st)
