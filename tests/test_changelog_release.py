import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, flags=re.MULTILINE)
    assert match, "pyproject.toml has no version"
    return match.group(1)


def test_has_current_release_heading():
    """The version in pyproject.toml must have a dated release section."""
    text = CHANGELOG.read_text(encoding="utf-8")
    version = _pyproject_version()
    assert re.search(
        rf"^## \[{re.escape(version)}\] — \d{{4}}-\d{{2}}-\d{{2}}$", text, flags=re.MULTILINE
    ), f"CHANGELOG.md has no dated release heading for {version}"


def test_has_fresh_unreleased_section():
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
    # Unreleased must appear ABOVE the current release section
    version = _pyproject_version()
    assert text.index("## [Unreleased]") < text.index(f"## [{version}]")
