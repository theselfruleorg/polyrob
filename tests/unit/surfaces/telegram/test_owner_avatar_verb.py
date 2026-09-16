"""`/avatar` — the owner sees the agent's face from the phone.

The Mindprint identity reached no chat surface at all. `pfp push` sets a profile
picture on X and Discord and prints BotFather steps for Telegram, but nothing
ever SHOWED the owner the face, its traits, or the voice signature.

⚠️ A Telegram verb is dead unless it joins FOUR lists — `dispatcher._COMMANDS`
(routing), `harness._OWNER_ADMIN_COMMANDS` (the owner gate), `_HELP_BODY` (the
help SSOT, which `help_commands()` parses to build the phone's "/" menu) and the
`cmd ==` dispatch branch. `test_the_verb_joined_every_list` is that contract;
the chat-first review of 2026-08-22 is where the three-list version of this bug
was found live.
"""
import json

import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind, _COMMANDS
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import (
    _OWNER_ADMIN_COMMANDS, act_on_inbound, help_commands)
from surfaces.telegram.inbound import InboundResult


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


def _cmd(command, text, user="alice"):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text=text,
                             identity=Identity(user_id=user, source=src,
                                               raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:" + user,
        command=command, session_id=None))


def _write_pfp(home, instance="rob", *, locked=True):
    d = home / "identity" / instance / "pfp"
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath("pfp.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
    d.joinpath("pfp.json").write_text(json.dumps({
        "generator": "mindprint@v2", "seed": "POLYROB", "variant": "#a1b2",
        "instance_id": instance, "seed_hex": "0x1546", "locked": locked,
        "traits": {"tier": "legendary", "eyes": "square", "mouth": "grin"},
        "voice": {"pitch": 1.29, "rate": 1.02, "timbre": 0.78},
    }))
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    return tmp_path


def test_the_verb_joined_every_list():
    assert "/avatar" in _COMMANDS, "not routable — dispatcher._COMMANDS"
    assert "/avatar" in _OWNER_ADMIN_COMMANDS, "not owner-gated"
    assert any(name == "avatar" for name, _desc in help_commands()), (
        "absent from _HELP_BODY, so it never reaches the phone's / menu")


@pytest.mark.asyncio
async def test_it_is_refused_for_a_non_owner(env):
    out = await act_on_inbound(_Agent(str(env)),
                               _cmd("/avatar", "/avatar", user="u_stranger"))
    assert "owner" in out.lower()


@pytest.mark.asyncio
async def test_it_reports_the_identity(env):
    _write_pfp(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/avatar", "/avatar"))
    assert "rob" in out
    assert "legendary" in out
    assert "1.29" in out, "the voice signature must be shown"


@pytest.mark.asyncio
async def test_a_draft_is_named_as_a_draft(env):
    _write_pfp(env, locked=False)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/avatar", "/avatar"))
    assert "draft" in out.lower()


@pytest.mark.asyncio
async def test_no_avatar_is_honest_and_names_the_setup_command(env):
    """Prod's real state on 2026-09-15 — the answer must be actionable, not a
    blank or a crash."""
    out = await act_on_inbound(_Agent(str(env)), _cmd("/avatar", "/avatar"))
    assert "not set up" in out.lower()
    assert "pfp generate" in out


@pytest.mark.asyncio
async def test_it_is_read_only_and_never_offers_to_change_the_identity(env):
    """`keep` is permanent. A chat verb must not be able to fire it."""
    _write_pfp(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/avatar", "/avatar keep"))
    meta = json.loads((env / "identity" / "rob" / "pfp" / "pfp.json").read_text())
    assert meta["locked"] is True  # unchanged
    assert "read-only" in out.lower() or "owner" in out.lower() or "usage" in out.lower()


def test_the_face_is_sent_as_a_photo_not_just_described(env):
    """The point of putting it on a chat surface is that the owner SEES it.
    The renderer must hand the PNG to the existing media_out rail."""
    from surfaces.telegram import owner_ops
    _write_pfp(env)
    text, png = owner_ops.avatar_reply(str(env), [])
    assert png and png.endswith("pfp.png"), (
        "the reply returns no image path, so nothing can be sent")
    # …and the harness hands that path to the media rail.
    import inspect
    from surfaces.telegram import harness
    src = inspect.getsource(harness._send_photo_best_effort)
    assert "media=" in src, "the photo never reaches an outbound media rail"


def test_the_capability_matrix_has_a_row():
    from tests.unit.test_surface_parity import CAPABILITY_MATRIX
    row = CAPABILITY_MATRIX.get("avatar")
    assert row, "no parity row — the verb can silently vanish from a surface"
    assert row[3] == "/avatar"
