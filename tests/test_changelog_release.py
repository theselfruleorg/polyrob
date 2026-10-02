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


# --- Release-section style (RELEASING.md, "Changelog and release notes") ------
#: Sections at or below this version predate the rules and are left as written.
STYLE_RULES_AFTER = (1, 1, 0)
STANDARD_HEADINGS = {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}
#: Names of internal work, not of shipped behavior.
WORK_ITEM = re.compile(
    r"\b(proposal \d+|WS-[A-Z0-9]+|wave \d+|P\d[a-z]?\b|0\d\d (validation|phase|P\d)"
    r"|not (yet )?deployed|deployed to prod)",
    re.IGNORECASE)


def _release_sections():
    text = CHANGELOG.read_text(encoding="utf-8")
    heads = list(re.finditer(r"^## \[(\d+)\.(\d+)\.(\d+)\][^\n]*$", text, flags=re.MULTILINE))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        link = re.search(r"^\[[0-9]+\.[0-9]+\.[0-9]+\]:", text[m.end():end], flags=re.MULTILINE)
        body = text[m.end(): m.end() + link.start()] if link else text[m.end():end]
        yield tuple(int(g) for g in m.groups()), body


def test_new_release_sections_use_the_six_standard_headings():
    for version, body in _release_sections():
        if version <= STYLE_RULES_AFTER:
            continue
        bad = [h for h in re.findall(r"^### (.+)$", body, flags=re.MULTILINE)
               if h.strip() not in STANDARD_HEADINGS]
        assert not bad, (
            f"{'.'.join(map(str, version))}: merge these into Added/Changed/Deprecated/"
            f"Removed/Fixed/Security (RELEASING.md): {bad[:5]}")


def test_new_release_sections_name_no_internal_work_items():
    for version, body in _release_sections():
        if version <= STYLE_RULES_AFTER:
            continue
        hits = [line.strip() for line in body.splitlines() if WORK_ITEM.search(line)]
        assert not hits, (
            f"{'.'.join(map(str, version))}: describe the shipped change, not the work "
            f"item (RELEASING.md): {hits[:3]}")


#: A bullet is at most 3 lines and about 40 words (RELEASING.md). Both caps apply to
#: ``[Unreleased]`` and to every release section after ``STYLE_RULES_AFTER``.
MAX_BULLET_LINES = 3
MAX_BULLET_CHARS = 300
#: Over-long bullets left in those sections when the caps landed (2026-10-02). A
#: RATCHET: it may only fall. Shorten a bullet, never raise this.
LONG_BULLETS_CEILING = 0


def _bullets(body: str):
    bullets, cur = [], None
    for line in body.splitlines():
        if line.startswith("- "):
            cur = [line]
            bullets.append(cur)
        elif line.startswith("  ") and cur is not None:
            cur.append(line)
        else:
            cur = None
    return bullets


def _unreleased_body() -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    m = re.search(r"^## \[Unreleased\][^\n]*\n(.*?)(?=^## \[)", text, flags=re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


def _capped_sections():
    yield "Unreleased", _unreleased_body()
    for version, body in _release_sections():
        if version > STYLE_RULES_AFTER:
            yield ".".join(map(str, version)), body


def _too_long(bullet) -> bool:
    flat = " ".join(line.strip() for line in bullet)
    return len(bullet) > MAX_BULLET_LINES or len(flat) > MAX_BULLET_CHARS


def test_bullets_stay_short():
    """RELEASING.md: a bullet is at most 3 lines; the detail belongs in the docs."""
    long_ones = [f"{name}: {b[0][:70]}" for name, body in _capped_sections()
                 for b in _bullets(body) if _too_long(b)]
    assert len(long_ones) <= LONG_BULLETS_CEILING, (
        f"a CHANGELOG bullet is at most {MAX_BULLET_LINES} lines and {MAX_BULLET_CHARS} "
        f"characters (RELEASING.md) — shorten it: {long_ones}")
