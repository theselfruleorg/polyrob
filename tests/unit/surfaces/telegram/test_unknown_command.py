"""030 WS-C4 (finding L9): an unknown/typo slash command must answer with help +
a suggestion in one cheap reply — never fall through to STEER/TASK_AGENT and burn
a full LLM turn. `/start` (the Telegram first-contact convention) gets a welcome.
`@botname` suffixes (Telegram group syntax) must not defeat command matching.
"""
import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind, route_inbound
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import _handle_command
from surfaces.telegram.inbound import InboundResult


class _FakeContainer:
    def get_service(self, name):
        return None


def _inbound(text, user="u_abc"):
    src = SessionSource(surface_id="telegram", chat_id="555", chat_type="dm")
    return InboundMessage(text=text, identity=Identity(user_id=user, source=src))


def _cmd(command, text, user_id="gleb"):
    decision = RouteDecision(kind=RouteKind.COMMAND, session_key="telegram:1",
                             session_id=None, command=command)
    return InboundResult(inbound=_inbound(text, user=user_id), decision=decision)


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


# --- routing: command-shaped unknown tokens are COMMANDS, not chat text -----

@pytest.mark.asyncio
async def test_unknown_slash_routes_as_command():
    d = await route_inbound(_FakeContainer(), _inbound("/wat"))
    assert d.kind == RouteKind.COMMAND
    assert d.command == "/wat"


@pytest.mark.asyncio
async def test_typo_of_a_real_verb_routes_as_command():
    d = await route_inbound(_FakeContainer(), _inbound("/statsu"))
    assert d.kind == RouteKind.COMMAND and d.command == "/statsu"


@pytest.mark.asyncio
async def test_start_routes_as_command():
    d = await route_inbound(_FakeContainer(), _inbound("/start"))
    assert d.kind == RouteKind.COMMAND and d.command == "/start"


@pytest.mark.asyncio
async def test_botname_suffix_is_stripped_for_known_commands():
    d = await route_inbound(_FakeContainer(), _inbound("/help@MyRobBot"))
    assert d.kind == RouteKind.COMMAND and d.command == "/help"


@pytest.mark.asyncio
async def test_non_command_shaped_slash_text_still_reaches_the_agent():
    """A path-like or prose-like leading slash is NOT a command attempt."""
    d = await route_inbound(_FakeContainer(), _inbound("/etc/passwd is the file I mean"))
    assert d.kind != RouteKind.COMMAND


# --- handler: cheap, teaching replies ---------------------------------------

@pytest.mark.asyncio
async def test_unknown_command_reply_names_the_verb_and_help(tmp_path):
    agent = _Agent(str(tmp_path))
    reply = await _handle_command(agent, _cmd("/wat", "/wat"), spawn=None)
    assert reply is not None
    assert "/wat" in reply
    assert "/help" in reply


@pytest.mark.asyncio
async def test_typo_gets_a_did_you_mean_suggestion(tmp_path):
    agent = _Agent(str(tmp_path))
    reply = await _handle_command(agent, _cmd("/statu", "/statu"), spawn=None)
    assert reply is not None and "/status" in reply


@pytest.mark.asyncio
async def test_start_gets_a_welcome_not_the_help_wall(tmp_path):
    agent = _Agent(str(tmp_path))
    reply = await _handle_command(agent, _cmd("/start", "/start"), spawn=None)
    assert reply is not None
    assert "/help" in reply
    # The welcome is a short first-contact message, not the full verb catalog.
    assert "/settle" not in reply


# --- 030 WS-C4: grouped /help + /help <verb> --------------------------------

@pytest.mark.asyncio
async def test_help_is_grouped(tmp_path):
    agent = _Agent(str(tmp_path))
    reply = await _handle_command(agent, _cmd("/help", "/help"), spawn=None)
    for section in ("— Tasks —", "— Control —", "— Money —"):
        assert section in reply


@pytest.mark.asyncio
async def test_help_verb_returns_only_that_verb(tmp_path):
    agent = _Agent(str(tmp_path))
    reply = await _handle_command(agent, _cmd("/help", "/help goal"), spawn=None)
    assert "/goal" in reply
    assert "/goal objective" in reply  # subverb lines included
    assert "/settle" not in reply      # not the whole catalog


@pytest.mark.asyncio
async def test_help_unknown_verb_suggests(tmp_path):
    agent = _Agent(str(tmp_path))
    reply = await _handle_command(agent, _cmd("/help", "/help statsu"), spawn=None)
    assert "Unknown command" in reply and "/status" in reply
