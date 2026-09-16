"""044 T18: `core.surfaces.group_admin` — the ONE owner-admin helper set every
seat (`/groups`, `polyrob owner groups`) renders through. Each function returns
ONE verified sentence; nothing here is rendered directly by a seat."""
import types

import pytest

from core.surfaces import group_admin


class _C:
    def __init__(self, tmp_path, *, with_services=True):
        self.config = types.SimpleNamespace(data_dir=str(tmp_path))
        if with_services:
            from core.surfaces.group_ledger import GroupLedger
            from core.surfaces.group_roles import GroupRoles
            self._svc = {"group_ledger": GroupLedger(str(tmp_path / "surfaces.db")),
                        "group_roles": GroupRoles(str(tmp_path / "surfaces.db"))}
        else:
            self._svc = {}

    def get_service(self, n):
        return self._svc.get(n)


def test_allow_here_writes_allowlist_and_name(tmp_path):
    c = _C(tmp_path)
    out = group_admin.allow_here(c, "telegram", "-1", "The Public Den", owner_uid="rob")
    assert "allowed" in out.lower() and "The Public Den" in out
    from core.surfaces.group_allowlist import GroupAllowlist
    assert GroupAllowlist(str(tmp_path / "group_allowlist.db")).is_allowed("telegram", "-1")


def test_allow_here_with_no_title_still_allows(tmp_path):
    c = _C(tmp_path)
    out = group_admin.allow_here(c, "telegram", "-2", "", owner_uid="rob")
    assert "allowed" in out.lower()


def test_deny_here_revokes(tmp_path):
    c = _C(tmp_path)
    group_admin.allow_here(c, "telegram", "-1", "Den", owner_uid="rob")
    out = group_admin.deny_here(c, "telegram", "-1")
    assert "denied" in out.lower()
    from core.surfaces.group_allowlist import GroupAllowlist
    assert not GroupAllowlist(str(tmp_path / "group_allowlist.db")).is_allowed("telegram", "-1")


def test_deny_here_when_not_active_says_so(tmp_path):
    c = _C(tmp_path)
    out = group_admin.deny_here(c, "telegram", "-999")
    assert "not active" in out.lower()


def test_set_mode_and_role_and_list(tmp_path):
    c = _C(tmp_path)
    group_admin.allow_here(c, "telegram", "-1", "Den", owner_uid="rob")
    assert "active" in group_admin.set_mode(c, "rob", "telegram", "-1", "active")
    assert "admin" in group_admin.set_role(c, "telegram", "-1", "9911", "admin", by="rob")
    assert "Den" in group_admin.list_rooms(c, "rob")


def test_bad_mode_is_a_sentence(tmp_path):
    c = _C(tmp_path)
    out = group_admin.set_mode(c, "rob", "telegram", "-1", "loud")
    assert "mention, active, listen, off" in out


def test_bad_role_echoes_the_vocabulary(tmp_path):
    c = _C(tmp_path)
    out = group_admin.set_role(c, "telegram", "-1", "9911", "superadmin", by="rob")
    assert "❌" in out
    assert "admin" in out and "member" in out and "blocked" in out


def test_list_rooms_when_none_allowed(tmp_path):
    c = _C(tmp_path)
    out = group_admin.list_rooms(c, "rob")
    assert "no group chats" in out.lower()


def test_set_key_writes_and_unset_clears(tmp_path):
    c = _C(tmp_path)
    out = group_admin.set_key(c, "rob", "telegram", "-1", "chat.tone", "playful")
    assert "chat.tone" in out and "playful" in out
    from core.surfaces.chat_policy import load
    assert load(tmp_path, "rob", "telegram", "-1").tone == "playful"
    out2 = group_admin.set_key(c, "rob", "telegram", "-1", "chat.tone", "unset")
    assert "unset" in out2.lower()
    assert load(tmp_path, "rob", "telegram", "-1").tone == ""


def test_set_key_unset_clears_a_value_a_bare_set_cannot(tmp_path):
    """`chat.quiet_hours` refuses an empty string at the coercer — `unset` is
    the only way to clear it."""
    c = _C(tmp_path)
    group_admin.set_key(c, "rob", "telegram", "-1", "chat.quiet_hours", "23-08")
    from core.surfaces.chat_policy import load
    assert load(tmp_path, "rob", "telegram", "-1").quiet_hours == "23-08"
    out = group_admin.set_key(c, "rob", "telegram", "-1", "chat.quiet_hours", "unset")
    assert out.startswith("✅")
    assert load(tmp_path, "rob", "telegram", "-1").quiet_hours == ""


def test_set_key_rejects_a_non_chat_key(tmp_path):
    c = _C(tmp_path)
    out = group_admin.set_key(c, "rob", "telegram", "-1", "style.tone", "x")
    assert "❌" in out


def test_tail_empty_then_populated(tmp_path):
    c = _C(tmp_path)
    out = group_admin.tail(c, "telegram", "-1", 30)
    assert "no ledger rows" in out.lower()

    import time as _time

    from core.surfaces.group_ledger import GroupLedger, LedgerRow
    ledger = c.get_service("group_ledger")
    ledger.append(LedgerRow(surface="telegram", chat_id="-1", thread_id=None,
                            message_id="1", ts=_time.time(), sender_id="9911",
                            sender_name="bob", sender_is_bot=False,
                            role_at_write="member", kind="text", text="hello room",
                            reply_to_message_id=None, mentions_bot=False))
    out2 = group_admin.tail(c, "telegram", "-1", 30)
    assert "bob" in out2 and "hello room" in out2


def test_tail_falls_back_to_a_fresh_handle_without_a_registered_service(tmp_path):
    c = _C(tmp_path, with_services=False)
    out = group_admin.tail(c, "telegram", "-1", 10)
    assert "no ledger rows" in out.lower()


def test_mute_writes_mute_until_and_mode_reads_as_listen(tmp_path):
    c = _C(tmp_path)
    out = group_admin.mute(c, "rob", "telegram", "-1", "2h")
    assert "muted" in out.lower() and "2h" in out
    from core.surfaces.chat_policy import load, mode_allows_trigger
    pol = load(tmp_path, "rob", "telegram", "-1")
    assert pol.mute_until > 0
    # A mute makes even a `mention`-mode room require owner/admin + addressed —
    # this is the live behaviour `chat_policy.mode_allows_trigger` already
    # implements (044 T17); confirm the write actually engages it.
    assert not mode_allows_trigger(pol, mentioned=False, role="member", wake_hit=False)
    assert not mode_allows_trigger(pol, mentioned=True, role="member", wake_hit=False)
    assert mode_allows_trigger(pol, mentioned=True, role="owner", wake_hit=False)


def test_mute_bad_duration_is_a_sentence(tmp_path):
    c = _C(tmp_path)
    out = group_admin.mute(c, "rob", "telegram", "-1", "soon")
    assert "bad duration" in out.lower()
