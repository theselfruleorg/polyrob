"""The `room_read` action — the agent can finally READ an allowlisted room.

Rob could POST to The Public Den but not read it (owner rail 2026-09-15 19:41Z:
"fix tg chat reading and deploy and restart"). The harness ALREADY captures
every allowlisted-room line into `group_ledger` (`core/surfaces/group_ledger.py`,
044 §4.1) — the room turn, the room-servicing goal and `/groups tail` all read
it — but an ordinary session had no action over it, so the agent probed dead
routes (t.me/s preview, anysite) and correctly reported "no read surface".

This action is that read surface, over the SAME store — no Telegram API is
involved (the Bot API cannot read history anyway), so the honest framing is
"the local room ledger", with its retention window stated.

⚠️ Read-only, and it never posts. Posting stays on `message` / the bound room
session. A refusal is structured (lists the allowlisted rooms), never a fake
empty read.
"""
import time

import pytest


class _Registry:
    def __init__(self):
        self.actions = {}

    def action(self, description, param_model=None, **kw):
        def deco(fn):
            self.actions[fn.__name__] = (fn, param_model, description)
            return fn
        return deco


class _Controller:
    def __init__(self, data_dir):
        self.registry = _Registry()
        self.container = type("C", (), {
            "config": type("Cfg", (), {"data_dir": str(data_dir)})()})()
        self.user_id = "rob"
        self.orchestrator = None


def _ctx():
    return type("Ctx", (), {"user_id": "rob", "role": "orchestrator",
                            "is_sub_agent": False, "session_id": "s",
                            "metadata": {}})()


def _seed_room(home, *, chat_id="-1001000000002", note="The Public Den (@example_den)"):
    from core.surfaces.group_allowlist import GroupAllowlist
    GroupAllowlist(str(home / "group_allowlist.db")).allow("telegram", chat_id, note)
    return chat_id


def _seed_lines(home, chat_id, rows):
    from core.surfaces.group_ledger import GroupLedger, LedgerRow
    ledger = GroupLedger(str(home / "surfaces.db"))
    base = time.time() - len(rows) * 60
    for i, (sender, text) in enumerate(rows):
        ledger.append(LedgerRow(
            surface="telegram", chat_id=chat_id, thread_id=None,
            message_id=str(i + 1), ts=base + i * 60, sender_id=str(100 + i),
            sender_name=sender, sender_is_bot=False, role_at_write="member",
            kind="text", text=text, reply_to_message_id=None, mentions_bot=False))
    return ledger


def _action(c):
    from tools.controller.room_read_action import register_room_read_action
    register_room_read_action(c)
    assert "room_read" in c.registry.actions, "action was not registered"
    fn, model, _ = c.registry.actions["room_read"]
    return fn, model


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    return tmp_path


@pytest.mark.asyncio
async def test_not_registered_when_rooms_off(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "false")
    c = _Controller(tmp_path)
    from tools.controller.room_read_action import register_room_read_action
    register_room_read_action(c)
    assert "room_read" not in c.registry.actions


@pytest.mark.asyncio
async def test_lists_rooms_when_no_room_given(home):
    chat = _seed_room(home)
    _seed_lines(home, chat, [("@a", "hello")])
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(), _ctx())
    out = res.extracted_content
    assert "-1001000000002" in out
    assert "The Public Den" in out
    assert "1 line" in out


@pytest.mark.asyncio
async def test_reads_lines_by_chat_id(home):
    chat = _seed_room(home)
    _seed_lines(home, chat, [
        ("@Nrmpap", "Keep doubting, let the believers keep making money"),
        ("@example_admin", "/unban"),
    ])
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(room="-1001000000002"), _ctx())
    out = res.extracted_content
    assert "@Nrmpap" in out and "Keep doubting" in out
    assert "@example_admin" in out and "/unban" in out
    # honest source framing — it is the local ledger, not a live history fetch
    assert "local room ledger" in out


@pytest.mark.asyncio
async def test_matches_room_by_title_fragment(home):
    chat = _seed_room(home)
    _seed_lines(home, chat, [("@a", "hi")])
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(room="public den"), _ctx())
    assert "hi" in res.extracted_content


@pytest.mark.asyncio
async def test_unknown_room_refuses_and_lists(home):
    chat = _seed_room(home)
    _seed_lines(home, chat, [("@a", "hi")])
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(room="-1009999999999"), _ctx())
    out = res.extracted_content
    assert "not an allowlisted room" in out
    assert "-1001000000002" in out, "refusal must list the real rooms"


@pytest.mark.asyncio
async def test_known_room_empty_ledger_is_honest(home):
    chat = _seed_room(home)
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(room=chat), _ctx())
    assert "no lines captured" in res.extracted_content


@pytest.mark.asyncio
async def test_empty_ledger_never_implies_own_posts_failed(home):
    """Prod 2026-09-19 18:25Z: the agent read `0 lines captured` for the public
    channel and told the owner it "can't be sure any channel post actually
    rendered" — while the owner was looking at the rendered posts. Telegram
    never delivers a bot's OWN messages back as updates, so an empty ledger
    says nothing about delivery; the send receipt is the confirmation. Both the
    room listing and the per-room read must say so.

    057 WS-E: that sentence is no longer written here — both sites CITE the one
    verification table, which is also what the SEND path renders. Pin the exact
    row rather than a substring, so a reworded table cannot quietly drop it."""
    from core.rails.verification import verification_line
    chat = _seed_room(home, chat_id="-1001000000001", note="Announcement channel (@example_channel)")
    c = _Controller(home)
    fn, model = _action(c)
    per_room = (await fn(model(room=chat), _ctx())).extracted_content
    listing = (await fn(model(room=None), _ctx())).extracted_content
    proof = verification_line("telegram_channel")
    assert "cannot read its own channel posts" in proof
    for out in (per_room, listing):
        assert proof in out, out
        assert "rails-verification.md" in out


@pytest.mark.asyncio
async def test_since_minutes_filters_old_lines(home):
    chat = _seed_room(home)
    from core.surfaces.group_ledger import GroupLedger, LedgerRow
    ledger = GroupLedger(str(home / "surfaces.db"))
    now = time.time()
    ledger.append(LedgerRow(
        surface="telegram", chat_id=chat, thread_id=None, message_id="1",
        ts=now - 7200, sender_id="1", sender_name="@old", sender_is_bot=False,
        role_at_write="member", kind="text", text="two hours old",
        reply_to_message_id=None, mentions_bot=False))
    ledger.append(LedgerRow(
        surface="telegram", chat_id=chat, thread_id=None, message_id="2",
        ts=now - 60, sender_id="2", sender_name="@new", sender_is_bot=False,
        role_at_write="member", kind="text", text="one minute old",
        reply_to_message_id=None, mentions_bot=False))
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(room=chat, since_minutes=30), _ctx())
    out = res.extracted_content
    assert "one minute old" in out
    assert "two hours old" not in out


# --- 057 WS-D: labels are labels ---------------------------------------------

def _set_chat_name(home, chat_id, name, monkeypatch, owner_uid="rob"):
    """Write chat.name under the OWNER tenant the action's own resolver reads."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", owner_uid)
    from core.instance import resolve_owner_user_id
    from core.surfaces import chat_policy
    ok, msg = chat_policy.set(str(home), resolve_owner_user_id(), "telegram",
                              chat_id, "chat.name", name)
    assert ok, msg


@pytest.mark.asyncio
async def test_note_is_never_rendered_as_the_room_title(home):
    """The owner's allowlist note is a LABEL. It must never occupy the quoted
    title position — that is the defect 057 R3 names (and the 09-19 'fix' only
    retyped the data)."""
    chat = _seed_room(home, note="ask before posting")
    _seed_lines(home, chat, [("@a", "hi")])
    c = _Controller(home)
    fn, model = _action(c)
    for res in (await fn(model(), _ctx()), await fn(model(room=chat), _ctx())):
        out = res.extracted_content
        # with no chat.name set, the room renders under its ADDRESS
        assert f'telegram:{chat} "telegram:{chat}"' in out
        # the note NEVER occupies the quoted title slot …
        assert f'telegram:{chat} "ask before posting"' not in out
        # … it renders behind an explicit `label:` marker, with its date
        assert 'label: "ask before posting" (set ' in out


@pytest.mark.asyncio
async def test_name_comes_from_chat_policy(home, monkeypatch):
    chat = _seed_room(home, note="ask before posting")
    _set_chat_name(home, chat, "The Public Den", monkeypatch)
    c = _Controller(home)
    fn, model = _action(c)
    out = (await fn(model(room=chat), _ctx())).extracted_content
    assert '"The Public Den"' in out
    assert 'label: "ask before posting"' in out
    assert out.index('"The Public Den"') < out.index('label:')


@pytest.mark.asyncio
async def test_a_room_is_still_findable_by_its_label(home):
    """Renaming the field must not make the owner's own words unusable as a
    search fragment — that would be a regression dressed as a fix."""
    chat = _seed_room(home, note="the den")
    _seed_lines(home, chat, [("@a", "hi")])
    c = _Controller(home)
    fn, model = _action(c)
    out = (await fn(model(room="the den"), _ctx())).extracted_content
    assert "@a" in out and "hi" in out
