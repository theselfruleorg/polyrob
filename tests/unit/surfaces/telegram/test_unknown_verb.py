"""113 W0 (F-04, C-03, C-04): a slash line no handler owns gets a true answer."""
import pytest

from core.surfaces.dispatcher import _command_token
from core.verbs import verb_for
from surfaces.telegram.harness import _help_for, _unknown_command_text
from surfaces.telegram.unknown_verb import elsewhere_text, unknown_command_text


def test_a_verb_of_another_seat_names_the_seat_instead_of_unknown():
    text = _unknown_command_text("/doctor")
    assert "Unknown command" not in text
    assert "It runs in the terminal, not here." in text
    assert _help_for("doctor") == text


def test_stop_and_settings_are_the_verbs_people_type():
    assert _command_token("/stop") == "/cancel"
    assert _command_token("/settings now") == "/prefs"
    assert verb_for("/stop").name == "/cancel"


def test_a_typo_still_gets_its_closest_verbs_and_a_real_verb_has_no_elsewhere_line():
    text = unknown_command_text("/statsu", ["/status", "/settle"], "telegram")
    assert text.startswith("Unknown command /statsu.") and "/status" in text
    assert elsewhere_text(verb_for("/status"), "telegram") == ""
    assert elsewhere_text(None, "telegram") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("line,expected", [("/h", "/help"), ("/cost", "/usage"), ("/stop", "/cancel")])
async def test_the_console_chat_box_resolves_an_alias(monkeypatch, line, expected):
    from webview import console_commands as cc
    seen = {}

    async def _route(task_agent, clean_id, user_id, text, token):
        seen["token"] = token
        return "ok"
    monkeypatch.setattr(cc, "_route_known_verb", _route)
    assert cc.looks_like_console_verb(line) is True
    assert await cc.maybe_handle_console_command(object(), "s1", "u1", line) == "ok"
    assert seen["token"] == expected


@pytest.mark.asyncio
async def test_the_console_names_the_terminal_for_a_terminal_verb():
    from webview import console_commands as cc
    reply = await cc.maybe_handle_console_command(object(), "s1", "u1", "/doctor")
    assert "It runs in the terminal, not here." in reply
