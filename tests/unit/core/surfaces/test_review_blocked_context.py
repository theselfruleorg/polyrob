"""Blocking a room member removes their history from model-facing reads."""
import time
from types import SimpleNamespace

from agents.task.goals.group_service import build_service_task, SKIP_NO_CHANGE
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.group_ledger import GroupLedger, LedgerRow
from core.surfaces.group_roles import GroupRoles
from core.surfaces.group_turn import build_room_turn, context_rows


def test_current_block_excludes_earlier_messages_but_keeps_audit(tmp_path):
    path = str(tmp_path / "surfaces.db")
    ledger, roles = GroupLedger(path), GroupRoles(path)
    services = {"group_ledger": ledger, "group_roles": roles}
    container = SimpleNamespace(get_service=services.get,
                                config=SimpleNamespace(data_dir=str(tmp_path)))
    row = LedgerRow("telegram", "-1", None, "1", time.time(), "8123", "Alice",
                    False, "member", "text", "attack-text", None, False)
    ledger.append(row)
    roles.grant("telegram", "-1", "8123", "blocked", granted_by="owner")
    assert context_rows(container, "telegram", "-1", [row]) == []
    inbound = InboundMessage(text="hi", identity=Identity(user_id="owner",
        raw_user_id="999", source=SessionSource("telegram", "-1", "supergroup")))
    turn = build_room_turn(container, inbound, role="owner", chat_name="room")
    assert "attack-text" not in turn.context
    task, skip = build_service_task(container,
        {"group": {"surface": "telegram", "chat_id": "-1"}}, owner_uid="owner", max_replies=3)
    assert task is None and skip == SKIP_NO_CHANGE
    assert ledger.tail("telegram", "-1")[0].text == "attack-text"


def test_unreadable_roles_do_not_expose_context(monkeypatch):
    import core.surfaces.group_admin as admin
    def broken(_):
        raise OSError("unreadable")
    monkeypatch.setattr(admin, "_roles", broken)
    row = SimpleNamespace(sender_id="8123", role_at_write="member")
    assert context_rows(None, "telegram", "-1", [row]) == []
