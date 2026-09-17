"""ONE Chromium launch policy (049 phase 4).

Every ``chromium.launch(`` / ``launch_persistent_context(`` in the package goes
through ``tools/browser/browser.py`` (the agent's browser) or is an owner
ceremony that consults ``tools/browser/launch_security.py`` /
``core/security/browser_rail.py`` first. A raw launch is a custody bypass with
the full process environment handed to a renderer of untrusted content — the
shape ``cli/commands/x_account.py`` had until 2026-09-17.

This list may only SHRINK.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGES = ("agents", "api", "cli", "core", "cron", "modules", "surfaces", "tools", "webview", "utils")
# A real call has a receiver (`pw.chromium.launch(`); prose in a docstring or
# comment writes `playwright.chromium.launch(env=…)` too, so require a code line
# that is not a comment and contains an assignment/await/return shape.
LAUNCH_RE = re.compile(r"^(?!\s*#).*(?:await\s+|=\s*|return\s+)\w+\.chromium\.launch(?:_persistent_context)?\(", re.M)

# Files that may call the launcher. Each MUST reference the policy in the same file.
ALLOWED = {
    "tools/browser/browser.py": "launch_security",
    "cli/commands/x_account.py": "desktop_launch_kwargs",
    "modules/pfp/renderer.py": "browser_rail_status",
}


def _launch_sites():
    for pkg in PACKAGES:
        for path in (REPO / pkg).rglob("*.py"):
            if "/tests/" in str(path) or "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if LAUNCH_RE.search(text):
                yield path.relative_to(REPO).as_posix(), text


def test_every_chromium_launch_consults_the_policy():
    unexpected = []
    unpoliced = []
    for rel, text in _launch_sites():
        if rel not in ALLOWED:
            unexpected.append(rel)
        elif ALLOWED[rel] not in text:
            unpoliced.append(rel)
    assert not unexpected, f"new raw Chromium launch site(s): {unexpected} — route through launch_security"
    assert not unpoliced, f"launch site(s) no longer consult the policy: {unpoliced}"


def test_allowlist_only_shrinks():
    present = {rel for rel, _ in _launch_sites()}
    stale = set(ALLOWED) - present
    assert not stale, f"remove retired rows from ALLOWED: {stale}"
