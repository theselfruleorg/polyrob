"""`/avatar` — the owner sees, sets and clears the agent's avatar from the phone.

The avatar is ONE image slot (`core/avatar.py`); core generates no face.

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


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def _write_pfp(home, instance="rob"):
    from core.avatar import set_avatar
    return set_avatar(home, instance, PNG, source="file:face.png").path


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
async def test_it_reports_the_avatar_and_its_source(env):
    _write_pfp(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/avatar", "/avatar"))
    assert "rob" in out
    assert "set (file:face.png)" in out


@pytest.mark.asyncio
async def test_an_unset_slot_shows_the_default_and_names_the_set_command(env):
    from core.avatar import DEFAULT_AVATAR
    from surfaces.telegram.owner_ops import avatar_reply
    text, img = avatar_reply(str(env), [])
    assert "default polyrob mark" in text and "/avatar set" in text
    assert img == str(DEFAULT_AVATAR)


@pytest.mark.asyncio
async def test_no_avatar_at_all_is_honest_and_names_the_set_command(env, monkeypatch):
    monkeypatch.setattr("core.avatar.DEFAULT_AVATAR", env / "missing.png")
    out = await act_on_inbound(_Agent(str(env)), _cmd("/avatar", "/avatar"))
    assert "not set" in out.lower()
    assert "/avatar set" in out


@pytest.mark.asyncio
async def test_an_unreadable_record_is_not_reported_as_absent(env):
    from core.avatar import avatar_dir
    _write_pfp(env)
    (avatar_dir(env, "rob") / "avatar.json").write_text("{broken")
    out = await act_on_inbound(_Agent(str(env)), _cmd("/avatar", "/avatar"))
    assert "unreadable" in out and "not set" not in out


@pytest.mark.asyncio
async def test_set_from_a_url_replaces_the_image(env, monkeypatch):
    from core.avatar import load_avatar
    from tools import avatar_sources

    async def _fake(url):
        return PNG + b"new", f"url:{url}"
    monkeypatch.setattr(avatar_sources, "image_from_url", _fake)
    out = await act_on_inbound(_Agent(str(env)),
                               _cmd("/avatar", "/avatar set https://x.test/a.png"))
    st = load_avatar(env, "rob")
    assert st.is_set and st.source == "url:https://x.test/a.png"
    assert "set (url:https://x.test/a.png)" in out
    assert "polyrob avatar push" in out  # the profile photos do not follow on their own


@pytest.mark.asyncio
async def test_a_refused_image_leaves_the_old_one(env, monkeypatch):
    from core.avatar import load_avatar
    from tools import avatar_sources

    async def _fake(url):
        return b"not an image", f"url:{url}"
    monkeypatch.setattr(avatar_sources, "image_from_url", _fake)
    _write_pfp(env)
    out = await act_on_inbound(_Agent(str(env)),
                               _cmd("/avatar", "/avatar set https://x.test/a.txt"))
    assert "NOT changed" in out
    assert load_avatar(env, "rob").source == "file:face.png"


@pytest.mark.asyncio
async def test_clear_empties_the_slot(env):
    from core.avatar import load_avatar
    _write_pfp(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/avatar", "/avatar clear"))
    assert "cleared" in out
    assert load_avatar(env, "rob").is_default


def test_set_runs_off_the_poll_loop():
    """A URL / NFT fetch must not hold the sequential Telegram poll loop."""
    from surfaces.telegram.harness import _runs_in_background
    assert _runs_in_background("/avatar", _cmd("/avatar", "/avatar set https://x"))
    assert not _runs_in_background("/avatar", _cmd("/avatar", "/avatar"))
    assert not _runs_in_background("/avatar", _cmd("/avatar", "/avatar clear"))


def test_the_face_is_sent_as_a_photo_not_just_described(env):
    """The point of putting it on a chat surface is that the owner SEES it.
    The renderer must hand the PNG to the existing media_out rail."""
    from surfaces.telegram import owner_ops
    _write_pfp(env)
    text, png = owner_ops.avatar_reply(str(env), [])
    assert png and png.endswith("avatar.png"), (
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


def _room_cmd(text, user="alice"):
    src = SessionSource("telegram", "-100", "group")
    inbound = InboundMessage(text=text,
                             identity=Identity(user_id=user, source=src,
                                               raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:group:-100", command="/avatar",
        session_id=None))


@pytest.mark.asyncio
@pytest.mark.parametrize("line", ["/avatar set https://x.test/a.png", "/avatar clear"])
async def test_a_change_is_refused_from_a_room(env, monkeypatch, line):
    """TG7 (audit 2026-10-03): `/avatar set|clear` changes this instance's
    face; like every other owner write it is never run from a room."""
    from core.avatar import load_avatar
    from tools import avatar_sources

    async def _fake(url):
        return PNG + b"new", f"url:{url}"
    monkeypatch.setattr(avatar_sources, "image_from_url", _fake)
    _write_pfp(env)
    out = await act_on_inbound(_Agent(str(env)), _room_cmd(line))
    assert "not available from a group chat" in out
    assert load_avatar(env, "rob").source == "file:face.png"


@pytest.mark.asyncio
async def test_the_read_in_a_room_posts_no_photo_into_the_room(env, monkeypatch):
    """TG7: the text answer goes to the owner's DM; the photo used to go to
    the ROOM's chat id."""
    sent = []

    async def _photo(task_agent, result, path):
        sent.append(path)
        return True
    monkeypatch.setattr("surfaces.telegram.harness._send_photo_best_effort", _photo)
    _write_pfp(env)
    out = await act_on_inbound(_Agent(str(env)), _room_cmd("/avatar"))
    assert "rob" in out
    assert sent == []
