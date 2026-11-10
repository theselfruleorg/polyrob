"""113 W4 item 9 (WP-20): one verb, one answer.

The reach half of parity is ``tests/unit/test_surface_parity.py`` (every seat HAS
the verb). This is the behaviour half: for each owner READ verb every seat runs,
the chat-app reply (``surfaces.telegram.harness._handle_command``) and the
terminal handler (``cli.ui.commands``) answer the SAME text for one empty world.

``BEHAVIOUR_DEBT`` names the verbs whose two answers differ today. It only
shrinks: a verb that now matches must leave the set (the test says so), and a
new read verb starts outside it. To clear a row, make the terminal handler wrap
the chat helper (``surfaces/telegram/*_reply``) or both wrap one core renderer.
"""
import asyncio
import io
import re

import pytest

from core import verbs

#: Verbs that act, or that need an argument to say anything: not reads.
_NOT_A_READ = frozenset({
    "/approve", "/reject", "/fulfill", "/more", "/goal", "/job", "/pause", "/resume",
    "/halt", "/run", "/dev", "/restart", "/update", "/cancel", "/allow", "/deny",
    "/config", "/new", "/chats", "/chat", "/rename", "/close", "/send", "/swap",
    "/bridge", "/deploy", "/launch", "/claim", "/pay", "/settle", "/model",
    "/groups", "/mute", "/unmute", "/ban", "/unban", "/paid", "/start", "/x",
    "/check", "/unquarantine", "/writeoff",
})
#: A seat-shaped answer BY DESIGN (the reason is the comment).
_SEAT_SHAPED = frozenset({
    "/help",    # each seat lists its own verbs
    "/cwd",     # the terminal has a run workspace; a chat app has none
    "/status",  # the terminal adds its run line under the one snapshot
})

#: The debt: read verbs whose terminal answer is not the chat answer yet.
BEHAVIOUR_DEBT = frozenset({
    "/mode",               # chat adds the health header
    "/usage",              # the terminal adds this run's meter above the same cost view
    # Both seats render ``owner_ops.invoices_reply``; each opens its own bot.db,
    # and this empty world has none, so the two "cannot read" lines differ.
    # tests/unit/cli/test_repl_owner_verbs.py proves one text over real rows.
    "/invoices",
    # The chat list reads the saved MCP store; the terminal reads its own live
    # MCP manager and the operator file (no store on a terminal install).
    "/mcp",
    # The same words (``owner_ops.avatar_reply``); a chat app sends the photo,
    # the terminal names the image file instead.
    "/avatar",
})


def read_verbs() -> list:
    return sorted(v.name for v in verbs.VERB_TABLE
                  if not v.seats and v.name not in _NOT_A_READ | _SEAT_SHAPED
                  and v.name not in verbs.pack_verb_names())


def _norm(text) -> str:
    """One line of words: no title rule, no status banner, no wrap, no markup."""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or re.fullmatch(r"-{3} .* -{3}", line) or line.startswith("■ "):
            continue
        out.append(re.sub(r"[.`*_]+", "", line))
    return re.sub(r"\s+", " ", " ".join(out)).casefold().strip()


class _Container:
    def __init__(self, home):
        self.config = type("Cfg", (), {"data_dir": home})()

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, home):
        self.container = _Container(home)


@pytest.fixture()
def world(monkeypatch, tmp_path):
    home = str(tmp_path / "data")
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("POLYROB_DATA_DIR", home)
    monkeypatch.setenv("DATA_ROOT", home)
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    return home


async def _chat_answer(home: str, verb: str) -> str:
    from core.surfaces.command_reply import reply_text
    from core.surfaces.dispatcher import RouteDecision, RouteKind
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    from surfaces.telegram.harness import _handle_command
    from surfaces.telegram.inbound import InboundResult
    source = SessionSource(surface_id="telegram", chat_id="1", chat_type="dm")
    inbound = InboundMessage(text=verb, identity=Identity(user_id="u_owner", source=source))
    decision = RouteDecision(kind=RouteKind.COMMAND, command=verb, session_id="s1",
                             session_key="agent:main:telegram:dm:1:u_owner")
    reply = await _handle_command(_Agent(home), InboundResult(inbound=inbound, decision=decision),
                                  spawn=None, deliver=None)
    return reply if isinstance(reply, str) else reply_text(reply)


async def _terminal_answer(home: str, verb: str) -> str:
    from cli.ui.commands import CommandContext, default_registry
    from cli.ui.plain_renderer import PlainRenderer
    from cli.ui.state import SessionState
    buf, state = io.StringIO(), SessionState()
    ctx = CommandContext(renderer=PlainRenderer(state=state, stream=buf), state=state,
                         container=_Container(home), task_agent=_Agent(home),
                         user_id="u_owner", session_id="s1")
    await default_registry().dispatch(verb, ctx)
    return buf.getvalue()


def _both(home: str, verb: str):
    async def run():
        return await _chat_answer(home, verb), await _terminal_answer(home, verb)
    chat, terminal = asyncio.run(run())
    return _norm(chat), _norm(terminal)


@pytest.mark.parametrize("verb", [v for v in read_verbs() if v not in BEHAVIOUR_DEBT])
def test_a_read_verb_answers_the_same_on_a_chat_app_and_in_the_terminal(world, verb):
    chat, terminal = _both(world, verb)
    assert chat and terminal == chat, (
        f"{verb} answers differently on the two seats. Make the terminal handler wrap "
        f"the chat helper.\n chat:     {chat[:400]}\n terminal: {terminal[:400]}")


@pytest.mark.parametrize("verb", sorted(BEHAVIOUR_DEBT))
def test_the_debt_only_shrinks(world, verb):
    assert verb in read_verbs(), f"{verb} is not a read verb any more: remove it from the debt"
    chat, terminal = _both(world, verb)
    assert chat != terminal, f"{verb} matches now: remove it from BEHAVIOUR_DEBT"


def test_the_debt_has_a_ceiling():
    assert len(BEHAVIOUR_DEBT) <= 5
