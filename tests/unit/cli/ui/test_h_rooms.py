"""C18 / E4 (2026-09-21) — the room + lifecycle verbs in the REPL.

``core.verbs`` has described ``/groups /mute /unmute /ban /unban /paid
/cancel /new /start`` on every seat for months; the terminal had none of them.
``h_rooms`` adds them as THIN calls into the SAME ``surfaces.telegram.group_ops``
helpers, with an honest stand-in for the Telegram routing result they read.

These tests pin the two things that could go wrong:
  * the stand-in must name the room the OWNER named (so a role check and the
    write it guards cannot disagree about which room they are about), and
  * reach must not become policy — the caller's role is ``owner`` only when
    ``core.instance.is_owner(uid, local=True)`` already says so.
"""
import asyncio
import io

import pytest

from cli.ui.commands import h_rooms
from cli.ui.commands.registry import CommandContext
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


def _ctx(args=None, user_id="local"):
    buf = io.StringIO()
    state = SessionState()
    ctx = CommandContext(renderer=PlainRenderer(state=state, stream=buf),
                         state=state, user_id=user_id, args=list(args or []))
    return ctx, buf


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# the stand-in
# ---------------------------------------------------------------------------


def test_chat_flag_names_the_room_and_is_removed_from_args():
    ctx, _ = _ctx(["mode", "--chat", "-100123", "off"])
    result, args, has_room = h_rooms._result(ctx, ctx.args)
    src = result.inbound.identity.source
    assert (src.surface_id, src.chat_id) == ("telegram", "-100123")
    assert "--chat" not in args and "-100123" not in args[2:]
    assert args[:2] == ["mode", "here"]      # the helper's own target token
    assert has_room


def test_chat_flag_accepts_an_explicit_surface():
    ctx, _ = _ctx(["mode", "--chat", "discord:42", "off"])
    result, _args, _ = h_rooms._result(ctx, ctx.args)
    src = result.inbound.identity.source
    assert (src.surface_id, src.chat_id) == ("discord", "42")


def test_explicit_pair_is_mirrored_into_the_standin():
    """The role stamp must describe the room the helper will actually resolve."""
    ctx, _ = _ctx(["mode", "telegram", "-100999", "off"])
    result, args, has_room = h_rooms._result(ctx, ctx.args)
    src = result.inbound.identity.source
    assert (src.surface_id, src.chat_id) == ("telegram", "-100999")
    assert args == ["mode", "telegram", "-100999", "off"]   # untouched
    assert has_room


def test_no_room_named_reads_as_a_dm():
    ctx, _ = _ctx(["list"])
    result, _args, has_room = h_rooms._result(ctx, ctx.args)
    assert result.inbound.identity.source.chat_type == "dm"
    assert not has_room


def test_owner_role_comes_from_is_owner_never_from_the_seat(monkeypatch):
    """REACH, never policy: a non-owner REPL is NOT stamped as the room owner."""
    monkeypatch.setattr("core.instance.is_owner", lambda uid, local=False: False)
    ctx, _ = _ctx(["list"])
    result, _a, _h = h_rooms._result(ctx, ctx.args)
    assert result.inbound.identity.chat_role is None

    monkeypatch.setattr("core.instance.is_owner", lambda uid, local=False: True)
    ctx, _ = _ctx(["list"])
    result, _a, _h = h_rooms._result(ctx, ctx.args)
    assert result.inbound.identity.chat_role == "owner"


# ---------------------------------------------------------------------------
# the verbs
# ---------------------------------------------------------------------------


def test_groups_list_needs_no_room():
    ctx, buf = _ctx(["list"])
    _run(h_rooms.h_groups(ctx))
    out = buf.getvalue()
    assert "--- groups ---" in out
    assert "this verb acts on a chat room" not in out


def test_paid_without_a_room_names_both_remedies():
    ctx, buf = _ctx([])
    _run(h_rooms.h_paid(ctx))
    out = buf.getvalue()
    assert "this verb acts on a chat room" in out
    assert "--chat" in out
    assert "/groups list" in out


def test_room_verb_failure_is_an_honest_line_not_a_crash(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("store exploded")
    monkeypatch.setattr("surfaces.telegram.group_ops.groups_reply", _boom)
    ctx, buf = _ctx(["list"])
    _run(h_rooms.h_groups(ctx))          # must not raise
    assert "groups unavailable" in buf.getvalue()


def test_room_reply_destination_is_named(monkeypatch):
    """A ``CommandReply`` bound for the ROOM is still shown — and says where it
    would have gone. Swallowing it is the silence this audit is about."""
    from core.surfaces.command_reply import CommandReply

    async def _reply(*a, **k):
        return CommandReply("price list", to_room=True)
    monkeypatch.setattr("surfaces.telegram.group_ops.paid_reply", _reply)
    ctx, buf = _ctx(["prices", "--chat", "-100123"])
    _run(h_rooms.h_paid(ctx))
    out = buf.getvalue()
    assert "price list" in out
    assert "goes to the room" in out


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_cancel_with_no_controller_is_honest():
    h_rooms._TURN.pop("ctrl", None)
    ctx, buf = _ctx([])
    h_rooms.h_cancel(ctx)
    assert "no interruptible turn loop" in buf.getvalue()


def test_cancel_interrupts_the_running_turn():
    class _Ctrl:
        busy = True

        def __init__(self):
            self.interrupted = False

        def interrupt(self):
            self.interrupted = True

    ctrl = _Ctrl()
    h_rooms.set_turn_controller(ctrl)
    try:
        ctx, buf = _ctx([])
        h_rooms.h_cancel(ctx)
        assert ctrl.interrupted
        assert "Task cancelled." in buf.getvalue()
    finally:
        h_rooms._TURN.pop("ctrl", None)


def test_cancel_when_idle_says_so():
    class _Ctrl:
        busy = False

        def interrupt(self):            # pragma: no cover - must not run
            raise AssertionError("interrupted an idle loop")

    h_rooms.set_turn_controller(_Ctrl())
    try:
        ctx, buf = _ctx([])
        h_rooms.h_cancel(ctx)
        assert "nothing is running" in buf.getvalue()
    finally:
        h_rooms._TURN.pop("ctrl", None)


def test_new_clears_and_states_the_session_is_unchanged(monkeypatch):
    cleared = {}
    monkeypatch.setattr("cli.ui.commands.handlers._h_clear",
                        lambda ctx: cleared.setdefault("yes", True))
    ctx, buf = _ctx([])
    ctx.session_id = "abc-123"
    h_rooms.h_new(ctx)
    out = buf.getvalue()
    assert cleared == {"yes": True}
    assert "Started fresh" in out
    assert "abc-123" in out            # no claim of a new session id


def test_start_prints_the_welcome():
    ctx, buf = _ctx([])
    h_rooms.h_start(ctx)
    out = buf.getvalue()
    assert "Welcome to polyrob" in out
    assert "/help" in out


def test_every_verb_is_registered():
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    for name in ("groups", "mute", "unmute", "ban", "unban", "paid",
                 "cancel", "new", "start"):
        assert reg.lookup(name) is not None, name


def test_cancel_is_a_live_command():
    """A stop verb the busy turn refuses to read is not a stop verb."""
    from cli.ui.input_policy import is_live_command
    assert is_live_command("/cancel")
