"""The `identity` status section — who the agent IS, on every status seat.

The avatar is ONE image slot (`core/avatar.py`): set (and from where), not
set, or unreadable. Core generates no face, so the section reports no
generator, trait or voice.

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


def _write_pfp(tmp_path, instance="rob"):
    from core.avatar import set_avatar
    return set_avatar(tmp_path, instance, b"\x89PNG\r\n\x1a\n" + b"x" * 8,
                      source="nft:base:0xabc:7").path


def test_a_set_avatar_reports_its_source(tmp_path):
    _write_pfp(tmp_path)
    sec = _identity_section("rob", str(tmp_path))
    assert sec.state == STATE_OK and sec.health == []
    assert sec.data["avatar"] == "set"
    assert sec.data["avatar_source"] == "nft:base:0xabc:7"
    blob = " ".join(sec.lines)
    assert "rob — avatar set (nft:base:0xabc:7)" in blob
    for gone in ("seed", "traits", "voice", "generator"):
        assert gone not in sec.data


def test_an_unset_slot_shows_the_default_and_is_OK(tmp_path):
    sec = _identity_section("rob", str(tmp_path))
    assert sec.state == STATE_OK
    assert sec.health == [], "the default avatar must not raise a health item"
    assert sec.data["avatar"] == "set" and sec.data["avatar_default"] is True
    assert "the default" in " ".join(sec.lines).lower()
    assert "polyrob avatar set" in " ".join(sec.lines)


def test_no_avatar_at_all_is_OK_and_says_so_rather_than_warning(tmp_path, monkeypatch):
    """No default shipped (a broken install): still optional, still honest."""
    monkeypatch.setattr("core.avatar.DEFAULT_AVATAR", tmp_path / "missing.png")
    sec = _identity_section("rob", str(tmp_path))
    assert sec.state == STATE_OK
    assert sec.health == [], "an optional setup step must not raise a health item"
    assert sec.data["avatar"] == "none"
    assert "not set" in " ".join(sec.lines).lower()
    assert "polyrob avatar set" in " ".join(sec.lines)


def test_no_avatar_still_names_the_instance(tmp_path):
    """Without an avatar the section must not go blank — the instance id is the
    identity fact that always exists."""
    sec = _identity_section("polyrob", str(tmp_path))
    assert "polyrob" in " ".join(sec.lines)


def test_an_unreadable_slot_is_reported_not_swallowed(tmp_path):
    from core.avatar import avatar_dir
    _write_pfp(tmp_path)
    (avatar_dir(tmp_path, "rob") / "avatar.json").write_text("{ not json")
    sec = _identity_section("rob", str(tmp_path))
    # An unreadable store is not an empty one: neither "set" nor "none".
    assert sec.data["avatar"] == "unreadable"
    assert "unreadable" in " ".join(sec.lines).lower()
    assert [h.key for h in sec.health] == ["avatar_unreadable"]


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


# --- reachable identities: email + X (2026-09-17) --------------------------

def _clear_reach_env(monkeypatch):
    for k in ("POLYROB_AGENT_EMAIL", "GMAIL_EMAIL", "AGENTMAIL_API_KEY",
              "TWITTER_API_KEY", "TWITTER_API_SECRET_KEY", "TWITTER_ACCESS_TOKEN",
              "TWITTER_ACCESS_TOKEN_SECRET", "TWITTER_BOT_USERNAME", "TWITTER_ENABLED"):
        monkeypatch.delenv(k, raising=False)


def test_identity_names_missing_email_and_x_with_remedies(tmp_path, monkeypatch):
    """A bootstrap skill must be able to READ 'no email / no X API' — and the
    remedy — from the one snapshot instead of guessing."""
    _clear_reach_env(monkeypatch)
    sec = _identity_section("rob", str(tmp_path))
    text = "\n".join(sec.lines)
    assert sec.data["email"] is None
    assert "email: none" in text and "AGENTMAIL_API_KEY" in text
    assert sec.data["x_api"] == "none"
    assert "x: no api keys" in text and "TWITTER_API_KEY" in text
    assert sec.state == STATE_OK  # optional setup raises no health item


def test_identity_reports_provisioned_inbox_and_x_api(tmp_path, monkeypatch):
    _clear_reach_env(monkeypatch)
    (tmp_path / "agent_mail.json").write_text(json.dumps(
        {"inbox_id": "i1", "address": "dangerob@agentmail.to"}))
    for k in ("TWITTER_API_KEY", "TWITTER_API_SECRET_KEY", "TWITTER_ACCESS_TOKEN",
              "TWITTER_ACCESS_TOKEN_SECRET"):
        monkeypatch.setenv(k, "x" * 20)
    monkeypatch.setenv("TWITTER_BOT_USERNAME", "dangerob")
    monkeypatch.setenv("TWITTER_ENABLED", "true")
    sec = _identity_section("dangerob", str(tmp_path))
    text = "\n".join(sec.lines)
    assert sec.data["email"] == "dangerob@agentmail.to"
    assert "email: dangerob@agentmail.to" in text
    assert sec.data["x_api"] == "configured" and sec.data["x_handle"] == "dangerob"
    assert "x: api configured @dangerob" in text and "writes ON" in text
    # secret hygiene: no key VALUE leaks into a status line
    assert "x" * 20 not in text


def test_identity_x_partial_names_the_missing_keys(tmp_path, monkeypatch):
    _clear_reach_env(monkeypatch)
    monkeypatch.setenv("TWITTER_API_KEY", "k")
    sec = _identity_section("rob", str(tmp_path))
    text = "\n".join(sec.lines)
    assert sec.data["x_api"] == "partial"
    assert "PARTIAL" in text and "TWITTER_ACCESS_TOKEN_SECRET" in text
