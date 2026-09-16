"""044 T16: per-chat roles (owner/admin/member/blocked).

Owner comes from the PRINCIPAL, never a row — so a blocked row on the owner's
own id can never demote him. No row means ``member``: the room regime is that
anyone may talk, and ``blocked`` is the only per-member deny.
"""
import pytest

from core.surfaces.group_roles import ROLES, GroupRoles


def test_default_is_member_and_owner_wins(tmp_path):
    r = GroupRoles(str(tmp_path / "s.db"))
    assert r.role("telegram", "-1", "u9", is_owner=False) == "member"
    assert r.role("telegram", "-1", "u9", is_owner=True) == "owner"


def test_grant_revoke_list(tmp_path):
    r = GroupRoles(str(tmp_path / "s.db"))
    r.grant("telegram", "-1", "u2", "admin", granted_by="owner")
    r.grant("telegram", "-1", "u3", "blocked", granted_by="owner", note="spam")
    assert r.role("telegram", "-1", "u2", is_owner=False) == "admin"
    assert r.role("telegram", "-1", "u3", is_owner=False) == "blocked"
    assert {x["user_id"]: x["role"] for x in r.list("telegram", "-1")} == {
        "u2": "admin", "u3": "blocked"}
    assert r.revoke("telegram", "-1", "u2") is True
    assert r.role("telegram", "-1", "u2", is_owner=False) == "member"


def test_revoke_of_a_row_that_was_never_there_is_false(tmp_path):
    """An honest answer, so a seat can say "nothing to revoke" instead of
    reporting a change that did not happen."""
    r = GroupRoles(str(tmp_path / "s.db"))
    assert r.revoke("telegram", "-1", "ghost") is False


def test_grant_is_an_upsert_not_a_second_row(tmp_path):
    r = GroupRoles(str(tmp_path / "s.db"))
    r.grant("telegram", "-1", "u2", "member", granted_by="owner")
    r.grant("telegram", "-1", "u2", "admin", granted_by="owner", note="promoted")
    rows = r.list("telegram", "-1")
    assert len(rows) == 1 and rows[0]["role"] == "admin" and rows[0]["note"] == "promoted"


def test_invalid_role_rejected(tmp_path):
    with pytest.raises(ValueError):
        GroupRoles(str(tmp_path / "s.db")).grant("telegram", "-1", "u", "root",
                                                 granted_by="owner")


def test_owner_row_cannot_be_blocked(tmp_path):
    r = GroupRoles(str(tmp_path / "s.db"))
    r.grant("telegram", "-1", "u_owner", "blocked", granted_by="admin")
    assert r.role("telegram", "-1", "u_owner", is_owner=True) == "owner"


def test_roles_are_scoped_to_one_chat(tmp_path):
    """A block in one room is not a block everywhere — the key is
    (surface, chat, user), so the same id is a member next door."""
    r = GroupRoles(str(tmp_path / "s.db"))
    r.grant("telegram", "-1", "u3", "blocked", granted_by="owner")
    assert r.role("telegram", "-2", "u3", is_owner=False) == "member"
    assert r.role("discord", "-1", "u3", is_owner=False) == "member"


def test_ids_are_compared_as_strings(tmp_path):
    """Telegram hands an int id; the seats hand a string. One row either way."""
    r = GroupRoles(str(tmp_path / "s.db"))
    r.grant("telegram", -1, 9911, "admin", granted_by="owner")
    assert r.role("telegram", "-1", "9911", is_owner=False) == "admin"


def test_role_vocabulary_is_the_one_list():
    assert ROLES == ("admin", "member", "blocked")
