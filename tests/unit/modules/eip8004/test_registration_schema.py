"""046 step 1: the served registration file must be TRUE and must be THIS agent's.

Three defects, none of which needs a chain to fix, and all of which made the
publicly-served `/eip8004/registration.json` lie:

- **Stale schema (I4).** The file emitted `endpoints`, which the current
  ERC-8004 draft renamed to `services`, and carried neither `x402Support` nor
  `active`. A consumer reading the spec finds none of the fields it expects.
- **A 404 image (I5).** `image` was hardcoded to
  `{base_url}/static/images/rob-logo.png` — a file that does not exist ANYWHERE
  in this tree. Every registration file ever served pointed at nothing.
- **Someone else's identity (I6).** `name="POLYROB"` and a fixed description
  were literals in framework code, so every instance advertised the framework's
  name instead of its own. This is the W1 neutral-identity rule: a specific
  bot's identity is DATA, never framework code.

⚠️ The `image` rule is the interesting one. When the instance has no public base
URL there is nowhere to host a face, and the fix is to OMIT `image` and carry the
avatar's seed instead — a broken image link is worse than no image, and the seed
keeps the face reproducible by anyone holding the open engine, which is a
stronger claim than a hosted PNG makes.
"""
import json

import pytest

from modules.eip8004.registration import build_registration_file


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for k in ("EIP8004_ONCHAIN_ENABLED", "EIP8004_AGENT_ID",
              "EIP8004_IDENTITY_REGISTRY", "X402_ENABLED", "MCP_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    return tmp_path


def _write_pfp(home, instance="rob"):
    d = home / "identity" / instance / "pfp"
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath("pfp.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    d.joinpath("pfp.json").write_text(json.dumps({
        "generator": "mindprint@v2", "seed": "POLYROB", "variant": "#a1b2",
        "seed_hex": "0x1546", "locked": True,
        "traits": {"tier": "rare"}, "voice": {"pitch": 1.1},
    }))
    return d


# --- I4: the current schema ------------------------------------------------

def test_the_file_emits_services_not_endpoints():
    reg = build_registration_file("https://example.test")
    dumped = reg.model_dump(exclude_none=True)
    assert "services" in dumped, "the draft renamed endpoints -> services"
    assert "endpoints" not in dumped


def test_the_file_declares_x402_support_and_active():
    reg = build_registration_file("https://example.test")
    dumped = reg.model_dump(exclude_none=True)
    assert "x402Support" in dumped
    assert "active" in dumped


def test_x402_support_reflects_whether_x402_is_actually_on(monkeypatch):
    """Advertising a payment rail that is switched off is the same class of lie
    as advertising an endpoint that 404s."""
    monkeypatch.setenv("X402_ENABLED", "false")
    assert build_registration_file("https://example.test").x402Support is False
    monkeypatch.setenv("X402_ENABLED", "true")
    assert build_registration_file("https://example.test").x402Support is True


def test_a_service_entry_keeps_the_endpoint_shape():
    """Renaming the LIST must not silently change each entry's fields."""
    reg = build_registration_file("https://example.test")
    assert reg.services, "no services at all — even A2A should be listed"
    first = reg.services[0].model_dump(exclude_none=True)
    assert "name" in first and "endpoint" in first


# --- I6: this agent's own identity -----------------------------------------

def test_the_name_is_not_a_framework_literal(monkeypatch):
    """⚠️ W1 neutral-identity: a specific bot's name is DATA, never code."""
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "acme_bot")
    reg = build_registration_file("https://example.test")
    assert "acme_bot" in reg.name.lower() or reg.name.lower() == "acme_bot"


def test_the_description_is_not_hardcoded_prose():
    import inspect

    from modules.eip8004 import registration
    src = inspect.getsource(registration)
    assert "browser control, file system access" not in src, (
        "the fixed marketing description is still a literal in framework code")


# --- I5: the image, or honestly nothing ------------------------------------

def test_the_image_is_this_instances_avatar_when_it_can_be_served(_clean):
    _write_pfp(_clean)
    reg = build_registration_file("https://example.test")
    assert reg.image == "https://example.test/pfp.png"


def test_the_image_is_never_the_missing_rob_logo():
    """That path does not exist anywhere in the tree — it was a guaranteed 404
    in every registration file this project has ever served."""
    reg = build_registration_file("https://example.test")
    assert "rob-logo" not in (reg.image or "")


def test_no_avatar_means_no_image_rather_than_a_broken_link(_clean):
    reg = build_registration_file("https://example.test")
    assert reg.image is None


def test_without_a_public_base_url_the_image_is_omitted(_clean):
    """A localhost URL is not resolvable by anyone reading the token."""
    _write_pfp(_clean)
    reg = build_registration_file("http://localhost:9000")
    assert reg.image is None


def test_the_seed_is_carried_so_the_face_stays_reproducible(_clean):
    """⚠️ Omitting the image must not lose the face. Anyone with the open engine
    re-renders it EXACTLY from generator+seed+variant."""
    _write_pfp(_clean)
    reg = build_registration_file("http://localhost:9000")
    dumped = reg.model_dump(exclude_none=True)
    blob = json.dumps(dumped)
    assert "mindprint@v2" in blob
    assert "#a1b2" in blob


def test_the_seed_is_absent_when_there_is_no_avatar(_clean):
    reg = build_registration_file("http://localhost:9000")
    blob = json.dumps(reg.model_dump(exclude_none=True))
    assert "mindprint" not in blob


# --- the honesty already pinned must survive -------------------------------

def test_trust_mode_is_still_local_by_default():
    assert build_registration_file("https://example.test").trustMode == "local"
