from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"

def test_has_0_4_2_release_heading():
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "## [0.4.2] — 2026-07-01" in text

def test_has_fresh_unreleased_section():
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
    # Unreleased must appear ABOVE the 0.4.2 section
    assert text.index("## [Unreleased]") < text.index("## [0.4.2]")
