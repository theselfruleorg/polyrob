"""The `identity` status section — who the agent IS, on every status seat.

The Mindprint avatar system (`avatar/`, `modules/pfp/`) has been complete and
well-tested since 2026-07-19 and reached almost nothing: no status surface
reported it, and `core.instance.voice_signature()` had ZERO callers outside
`polyrob pfp say`. Verified on prod 2026-09-15: Rob #1 had no avatar at all and
no seat said so.

Adding it to the ONE snapshot (rather than to a seventh renderer) is what puts
it on Telegram `/status` and `/mode`, `polyrob doctor`, `polyrob autonomy
status`, the webview /system page, the daily digest and the agent's own
`agent_status` in a single change.

⚠️ An absent avatar is `ok`, NOT `degraded`. Setup is genuinely optional, and a
permanent WARN for an optional step is the noise that teaches an owner to skip
the health block — the exact failure the status SSOT exists to prevent.
"""
import json

import pytest

from core.status_snapshot import STATE_OK, STATE_UNAVAILABLE, _identity_section


def _write_pfp(tmp_path, instance="rob", *, locked=True, **over):
    d = tmp_path / "identity" / instance / "pfp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pfp.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    meta = {
        "generator": "mindprint@v2", "seed": "POLYROB", "variant": "#a1b2",
        "instance_id": instance, "seed_hex": "0x1546", "locked": locked,
        "traits": {"tier": "rare", "eyes": "square", "mouth": "grin",
                   "head": "orb", "antenna": "single", "aura": "none",
                   "brow": "none", "mode": "solid"},
        "voice": {"pitch": 1.29, "rate": 1.02, "timbre": 0.78},
        "rendered_by": "pillow-mesh",
    }
    meta.update(over)
    (d / "pfp.json").write_text(json.dumps(meta))
    return d


def test_a_kept_avatar_reports_its_identity(tmp_path):
    _write_pfp(tmp_path)
    sec = _identity_section("rob", str(tmp_path))
    assert sec.state == STATE_OK
    assert sec.data["avatar"] == "kept"
    assert sec.data["seed_hex"] == "0x1546"
    assert sec.data["tier"] == "rare"
    blob = " ".join(sec.lines)
    assert "rob" in blob and "0x1546" in blob and "rare" in blob


def test_the_voice_signature_is_surfaced(tmp_path):
    """Its first consumer outside `pfp say`. A voice the agent cannot report is
    a voice nothing can use."""
    _write_pfp(tmp_path)
    sec = _identity_section("rob", str(tmp_path))
    assert sec.data["voice"] == {"pitch": 1.29, "rate": 1.02, "timbre": 0.78}
    assert "1.29" in " ".join(sec.lines)


def test_a_draft_avatar_says_it_is_not_kept_yet(tmp_path):
    _write_pfp(tmp_path, locked=False)
    sec = _identity_section("rob", str(tmp_path))
    assert sec.data["avatar"] == "draft"
    assert "draft" in " ".join(sec.lines).lower()


def test_no_avatar_is_OK_and_says_so_rather_than_warning(tmp_path):
    """Prod's actual state on 2026-09-15."""
    sec = _identity_section("rob", str(tmp_path))
    assert sec.state == STATE_OK
    assert sec.health == [], "an optional setup step must not raise a health item"
    assert sec.data["avatar"] == "none"
    assert "not set up" in " ".join(sec.lines).lower()


def test_no_avatar_still_names_the_instance(tmp_path):
    """Without an avatar the section must not go blank — the instance id is the
    identity fact that always exists."""
    sec = _identity_section("polyrob", str(tmp_path))
    assert "polyrob" in " ".join(sec.lines)


def test_an_unreadable_meta_is_reported_not_swallowed(tmp_path):
    d = tmp_path / "identity" / "rob" / "pfp"
    d.mkdir(parents=True)
    (d / "pfp.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "pfp.json").write_text("{ not json")
    sec = _identity_section("rob", str(tmp_path))
    # The PNG exists but its record does not parse: that is neither "kept" nor
    # "none", and reporting either one would be a confident lie.
    assert sec.data["avatar"] == "unreadable"
    assert "unreadable" in " ".join(sec.lines).lower()


def test_the_section_never_creates_the_identity_directory(tmp_path):
    """Status is a READ. A status surface that creates state is how an empty
    store becomes a real one."""
    _identity_section("rob", str(tmp_path))
    assert not (tmp_path / "identity").exists()


def test_a_missing_data_dir_is_unavailable_not_a_crash():
    """The section function RAISES and `_guarded` types it — the module-wide
    contract, so an unreadable source renders its reason instead of a zero."""
    from core.status_snapshot import _guarded
    sec = _guarded("identity", _identity_section, "rob", None)
    assert sec.state == STATE_UNAVAILABLE
    assert sec.reason and "data dir" in sec.reason


# --- wiring into the snapshot ---------------------------------------------

def test_identity_is_in_the_section_order_and_titled():
    from core.status_snapshot import SECTION_ORDER
    from core.status_render import _SECTION_TITLES
    assert "identity" in SECTION_ORDER
    assert _SECTION_TITLES.get("identity")


def test_the_snapshot_always_carries_an_identity_section(tmp_path):
    """Every section is typed and ALWAYS present — the status SSOT invariant."""
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("rob", data_dir=str(tmp_path))
    assert "identity" in snap.sections
    assert snap.sections["identity"].name == "identity"


def test_the_renderer_emits_the_identity_block(tmp_path):
    from core.status_snapshot import build_status_snapshot
    from core.status_render import render_section_lines
    _write_pfp(tmp_path, instance="rob")
    snap = build_status_snapshot("rob", data_dir=str(tmp_path))
    lines = render_section_lines(snap, "identity")
    assert lines and any("Identity" in ln for ln in lines)


# --- ERC-8004 on-chain identity (046) --------------------------------------

def _write_erc8004(tmp_path, instance="rob", **over):
    from core.instance import erc8004_record_path
    p = erc8004_record_path(tmp_path, instance)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"chain": "base", "chain_id": 8453,
           "registry": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
           "agent_id": 42, "tx_hash": "0x" + "ab" * 32,
           "registered_at": "2026-09-15T00:00:00+00:00"}
    doc.update(over)
    p.write_text(json.dumps(doc))
    return p


def test_a_registered_agent_shows_its_agent_id(tmp_path):
    _write_pfp(tmp_path)
    _write_erc8004(tmp_path)
    sec = _identity_section("rob", str(tmp_path))
    assert sec.data["erc8004"]["agent_id"] == 42
    blob = " ".join(sec.lines)
    assert "42" in blob and "base" in blob


def test_an_unregistered_agent_says_so_rather_than_going_silent(tmp_path):
    """An absent identity is a fact the owner should be able to read, not an
    empty space — the same rule the avatar line follows."""
    _write_pfp(tmp_path)
    sec = _identity_section("rob", str(tmp_path))
    assert sec.data["erc8004"] is None
    assert "not registered" in " ".join(sec.lines).lower()


def test_being_unregistered_raises_no_health_item(tmp_path):
    """Optional, like the avatar. A permanent WARN for an optional step is the
    noise that teaches an owner to skip the health block."""
    _write_pfp(tmp_path)
    sec = _identity_section("rob", str(tmp_path))
    assert sec.health == []
