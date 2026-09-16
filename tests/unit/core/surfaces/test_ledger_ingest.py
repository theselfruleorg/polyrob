import time
import types

from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.group_allowlist import GroupAllowlist
from core.surfaces.group_ledger import GroupLedger
from core.surfaces.ledger_ingest import record_inbound_to_ledger


class _C:
    def __init__(self, tmp_path, ledger):
        self._svc = {"group_ledger": ledger}
        self.config = types.SimpleNamespace(data_dir=str(tmp_path))
    def get_service(self, n):
        return self._svc.get(n)


def _inb(chat_type="supergroup", text="hi", mid=5):
    src = SessionSource(surface_id="telegram", chat_id="-1", chat_type=chat_type)
    ident = Identity(user_id="u_x", source=src, raw_user_id="8123", display_name="alice")
    # A live timestamp, not a stale fixed literal: group_ledger's retention is
    # wall-clock (round-1 review) and prunes on every read as well as every
    # append, so a hardcoded old date would be silently pruned out from under
    # this fixture the moment it aged past GROUP_LEDGER_RETENTION_DAYS.
    raw = {"message": {"message_id": mid, "date": time.time(), "from": {"id": 8123, "username": "alice"}, "text": text}}
    return InboundMessage(text=text, identity=ident, raw=raw)


def test_allowed_room_message_is_recorded(tmp_path):
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("telegram", "-1")
    lg = GroupLedger(str(tmp_path / "s.db"))
    assert record_inbound_to_ledger(_C(tmp_path, lg), _inb(), role="member", is_owner=False)
    rows = lg.tail("telegram", "-1")
    assert rows[0].sender_name == "@alice" and rows[0].sender_id == "8123" and rows[0].text == "hi"


def test_unlisted_room_is_not_recorded(tmp_path):
    lg = GroupLedger(str(tmp_path / "s.db"))
    assert not record_inbound_to_ledger(_C(tmp_path, lg), _inb(), role="member", is_owner=False)
    assert lg.count("telegram", "-1") == 0


def test_dm_is_never_recorded(tmp_path):
    lg = GroupLedger(str(tmp_path / "s.db"))
    assert not record_inbound_to_ledger(_C(tmp_path, lg), _inb(chat_type="dm"), role="owner", is_owner=True)


def test_channel_post_with_no_from_falls_back_to_chat_identity(tmp_path):
    # A channel_post carries no `from` at all (Telegram channels post as the
    # channel, not a user) -- build_inbound_message's own fallback is
    # raw_user_id=chat_id, display_name=None in that case, so the ingest
    # helper must still record the line rather than drop it.
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("telegram", "-1")
    lg = GroupLedger(str(tmp_path / "s.db"))
    src = SessionSource(surface_id="telegram", chat_id="-1", chat_type="channel")
    ident = Identity(user_id="u_c", source=src, raw_user_id="-1", display_name=None)
    raw = {"channel_post": {"message_id": 9, "date": time.time(), "text": "announcement"}}
    inb = InboundMessage(text="announcement", identity=ident, raw=raw)
    assert record_inbound_to_ledger(_C(tmp_path, lg), inb, role="member", is_owner=False)
    rows = lg.tail("telegram", "-1")
    assert rows[0].sender_id == "-1" and rows[0].sender_name == "" and rows[0].text == "announcement"


# ---------------------------------------------------------------------------
# 044 M-a — a join/leave service message is an EVENT, never a blank member line
# ---------------------------------------------------------------------------

def _service_inb(field, value, mid=7):
    src = SessionSource(surface_id="telegram", chat_id="-1", chat_type="supergroup")
    ident = Identity(user_id="u_x", source=src, raw_user_id="8123", display_name="alice")
    raw = {"message": {"message_id": mid, "date": time.time(),
                       "from": {"id": 8123, "username": "alice"}, field: value}}
    return InboundMessage(text="", identity=ident, raw=raw)


def test_join_is_recorded_as_a_named_service_event(tmp_path):
    """The ingest runs BEFORE the harness's empty-content guard, so a service
    message was appended as kind="text" with an empty body: the room context then
    rendered `@alice|8123 (member): ` — a member who said nothing."""
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("telegram", "-1")
    lg = GroupLedger(str(tmp_path / "s.db"))
    inb = _service_inb("new_chat_members", [{"id": 99, "username": "bob"}])
    assert record_inbound_to_ledger(_C(tmp_path, lg), inb, role="member", is_owner=False)
    row = lg.tail("telegram", "-1")[0]
    assert row.kind == "service"
    assert row.text == "joined the room"


def test_leave_is_recorded_as_a_named_service_event(tmp_path):
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("telegram", "-1")
    lg = GroupLedger(str(tmp_path / "s.db"))
    inb = _service_inb("left_chat_member", {"id": 99})
    assert record_inbound_to_ledger(_C(tmp_path, lg), inb, role="member", is_owner=False)
    row = lg.tail("telegram", "-1")[0]
    assert row.kind == "service" and row.text == "left the room"


def test_an_empty_non_service_message_is_never_a_blank_line(tmp_path):
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("telegram", "-1")
    lg = GroupLedger(str(tmp_path / "s.db"))
    assert not record_inbound_to_ledger(_C(tmp_path, lg), _inb(text="  "),
                                        role="member", is_owner=False)
    assert lg.count("telegram", "-1") == 0


def test_an_ordinary_text_line_is_unchanged(tmp_path):
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("telegram", "-1")
    lg = GroupLedger(str(tmp_path / "s.db"))
    assert record_inbound_to_ledger(_C(tmp_path, lg), _inb(text="hello"),
                                    role="member", is_owner=False)
    row = lg.tail("telegram", "-1")[0]
    assert row.kind == "text" and row.text == "hello"


# ---------------------------------------------------------------------------
# 044 I12 — a non-Telegram room goes silent; say so, once, naming the surface
# ---------------------------------------------------------------------------

def test_a_surface_with_no_message_id_warns_once_naming_itself(tmp_path, caplog):
    """The ingest reads a TELEGRAM-shaped raw (`message.message_id`), so a
    Discord/Slack/Signal room recorded NO lines at all and the failure was a
    DEBUG-level `return False`: the room's context block stayed permanently empty
    and its service job permanently reported no-change."""
    import logging
    from core.surfaces import ledger_ingest
    ledger_ingest._NO_MESSAGE_ID_WARNED.clear()
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-9")
    lg = GroupLedger(str(tmp_path / "s.db"))
    src = SessionSource(surface_id="discord", chat_id="chan-9", chat_type="group")
    ident = Identity(user_id="u_x", source=src, raw_user_id="8123")
    inb = InboundMessage(text="hi", identity=ident, raw={"id": "discord-native-id"})

    with caplog.at_level(logging.WARNING):
        assert not record_inbound_to_ledger(_C(tmp_path, lg), inb, role="member",
                                            is_owner=False)
        assert not record_inbound_to_ledger(_C(tmp_path, lg), inb, role="member",
                                            is_owner=False)
    warnings = [r for r in caplog.records
                if r.levelno >= logging.WARNING and "room ledger" in r.getMessage()]
    assert len(warnings) == 1, "the warning must fire ONCE per surface, not per line"
    assert "discord" in warnings[0].getMessage()
    ledger_ingest._NO_MESSAGE_ID_WARNED.clear()
