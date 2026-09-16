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
    assert text.count("## [Unreleased]") == 1
    # Unreleased must appear ABOVE the current release section
    version = _pyproject_version()
    assert text.index("## [Unreleased]") < text.index(f"## [{version}]")


def test_no_release_notes_before_unreleased_section():
    """Every pending note must be inside Unreleased so export stripping sees it."""
    text = CHANGELOG.read_text(encoding="utf-8")
    before = text[:text.index("## [Unreleased]")]
    assert not re.search(r"^### ", before, flags=re.MULTILINE), (
        "release notes appear before ## [Unreleased]; the public exporter would "
        "leave them outside the release section"
    )


def test_no_release_notes_after_link_definitions():
    """Nothing may be appended below the end-of-file release-link block."""
    text = CHANGELOG.read_text(encoding="utf-8")
    link = re.search(r"^\[[0-9]+\.[0-9]+\.[0-9]+\]:", text, flags=re.MULTILINE)
    if link is None:
        return
    after = text[link.start():]
    stray = [line for line in after.splitlines()
             if line.strip() and not re.match(
                 r"^\[[0-9]+\.[0-9]+\.[0-9]+\]:\s+https://", line)]
    assert not stray, (
        "content appears after the changelog link definitions; move it into "
        f"## [Unreleased]: {stray[:3]}"
    )
