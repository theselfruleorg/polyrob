"""avatar/mindprint.js is the SSOT engine; the portal keeps a copy for its static
site. They MUST stay byte-identical so the logo/brand and the app avatars are one
visual system. This guard runs everywhere (no node needed)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_portal_engine_copy_matches_ssot():
    ssot = (ROOT / "avatar" / "mindprint.js").read_bytes()
    portal = (ROOT / "web" / "portal" / "brand" / "mindprint-engine.js").read_bytes()
    assert ssot == portal, "avatar/mindprint.js and web/portal/brand/mindprint-engine.js drifted"
