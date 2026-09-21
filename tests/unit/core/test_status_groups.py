"""The `groups` status section (044 T18): rooms this instance is present in.

Instance-level (the allowlist has no `user_id` column — one bot presence per
chat, not per-tenant), read-only, existence-guarded: an absent allowlist is
"no rooms", never created by a read.
"""
import os

from core.status_snapshot import STATE_UNAVAILABLE, _groups_section, build_status_snapshot


def test_no_allowlist_reads_as_no_rooms_never_created(tmp_path):
    sec = _groups_section("owner", str(tmp_path))
    assert sec.lines == ["no rooms"]
    assert sec.data["rooms"] == []
    assert not os.path.exists(os.path.join(str(tmp_path), "group_allowlist.db"))


def test_an_allowed_room_is_reported_with_its_name_and_mode(tmp_path, monkeypatch):
    from core.surfaces import group_admin
    import types
    # `_groups_section` reads a room's overlay via `chat_policy.load_for_chat`,
    # which resolves the OWNER principal itself (never the caller's `user_id`)
    # — pin it so the write (below) and that read agree on the same tenant.
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    container = types.SimpleNamespace(
        config=types.SimpleNamespace(data_dir=str(tmp_path)),
        get_service=lambda n: None)
    group_admin.allow_here(container, "telegram", "-1", "The Public Den", owner_uid="owner")
    group_admin.set_mode(container, "owner", "telegram", "-1", "active")

    sec = _groups_section("owner", str(tmp_path))
    # `allow_here`/`set_mode` never touch `surfaces.db` (only the allowlist and
    # a `chat.*` overlay file) — the room ledger/caps store genuinely doesn't
    # exist yet, so this also covers Important 2's guarded shape.
    rooms = sec.data["rooms"]
    # 057 WS-D: the allowlist note is a dated LABEL beside the name, never the
    # title — `allow_here` wrote the same string to both homes, so they agree here.
    label = rooms[0].pop("label")
    assert label.startswith('label: "The Public Den" (set 20')
    assert rooms == [
        {"surface": "telegram", "chat_id": "-1", "name": "The Public Den",
         "mode": "active", "replies_today": 0, "ledger_rows": 0, "last_write": None,
         "note": "surfaces.db absent"},
    ]
    assert any("The Public Den" in ln and "mode=active" in ln
              and "surfaces.db absent" in ln for ln in sec.lines)
    assert not os.path.exists(os.path.join(str(tmp_path), "surfaces.db"))


def test_a_revoked_room_is_not_listed(tmp_path):
    from core.surfaces.group_allowlist import GroupAllowlist
    allow = GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db"))
    allow.allow("telegram", "-1", note="Den")
    allow.revoke("telegram", "-1")

    sec = _groups_section("owner", str(tmp_path))
    assert sec.lines == ["no rooms"]


def test_it_is_instance_level_not_tenant_scoped(tmp_path):
    from core.surfaces.group_allowlist import GroupAllowlist
    GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db")).allow(
        "telegram", "-1", note="Den")

    a = _groups_section("owner-a", str(tmp_path))
    b = _groups_section("owner-b", str(tmp_path))
    assert [r["chat_id"] for r in a.data["rooms"]] == ["-1"]
    assert [r["chat_id"] for r in b.data["rooms"]] == ["-1"]


def test_the_section_is_in_the_pinned_order():
    from core.status_snapshot import SECTION_ORDER
    assert "groups" in SECTION_ORDER


def test_it_is_present_in_the_full_snapshot(tmp_path):
    snap = build_status_snapshot("owner", data_dir=str(tmp_path), include_money=False)
    assert snap.sections["groups"].state != STATE_UNAVAILABLE
    assert snap.sections["groups"].lines == ["no rooms"]


# ---------------------------------------------------------------------------
# 044 T18 fix round 1
# ---------------------------------------------------------------------------

def test_reading_the_section_never_creates_surfaces_db(tmp_path):
    """Important 2: `GroupLedger`/`RoomCaps.__init__` run DDL — constructing
    either on a plain status read would CREATE `surfaces.db`. An active room
    seeded straight through the allowlist (no `group_admin` call, so nothing
    else could have created it either) proves the guard, not just the earlier
    incidental absence in the round-trip test above."""
    from core.surfaces.group_allowlist import GroupAllowlist
    surfaces_db = os.path.join(str(tmp_path), "surfaces.db")
    GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db")).allow(
        "telegram", "-1", note="Den")
    assert not os.path.exists(surfaces_db)

    sec = _groups_section("owner", str(tmp_path))

    assert not os.path.exists(surfaces_db)
    assert sec.data["rooms"][0]["note"] == "surfaces.db absent"
    assert sec.data["rooms"][0]["ledger_rows"] == 0
    assert sec.data["rooms"][0]["replies_today"] == 0


def test_surfaces_db_present_reports_real_counts_with_no_note(tmp_path):
    from core.surfaces.group_allowlist import GroupAllowlist
    from core.surfaces.group_ledger import GroupLedger
    GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db")).allow(
        "telegram", "-1", note="Den")
    GroupLedger(os.path.join(str(tmp_path), "surfaces.db"))  # creates the file

    sec = _groups_section("owner", str(tmp_path))
    assert sec.data["rooms"][0]["note"] == ""


def test_unreadable_allowlist_renders_unavailable_with_a_reason(tmp_path):
    """Important 6: a corrupt/unreadable `group_allowlist.db` must not vanish
    the section — `_guarded` (the SAME wrapper every other section uses) turns
    the construction failure into a typed `unavailable(<reason>)`."""
    from core.status_snapshot import STATE_UNAVAILABLE, _guarded
    db_path = os.path.join(str(tmp_path), "group_allowlist.db")
    with open(db_path, "wb") as fh:
        fh.write(b"not a sqlite database at all")

    sec = _guarded("groups", _groups_section, "owner", str(tmp_path))

    assert sec.state == STATE_UNAVAILABLE
    assert sec.reason
    assert sec.name == "groups"  # the section is still PRESENT, just unavailable
