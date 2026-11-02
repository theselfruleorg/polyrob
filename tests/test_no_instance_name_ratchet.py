"""The shipped tree names no instance: the product is POLYROB, an install names its agent.

Owner rule (2026-10-31, proposal 095 D10): core ships mechanisms, never the name or
the settings of one install. The name of the instance this project runs on was
spread through owner copy ("<name> is paused."), help text, docstrings, token
examples, persona fixtures and test ids. Owner copy says "the agent"; an example
token is "Demo Coin"; a test owner is "alice".

Scope is every directory and file the public export ships (tracked files only);
history (every ``CHANGELOG*.md``) is exempt. The one legacy spelling that stays is
functional: the read-only fallback home ``~/.<name>`` and its one-time migration to
``~/.polyrob`` / ``identity/polyrob`` — removing it would strand an old install's
keys. Those spans are stripped before the match; the few other lines that live data
needs (old API keys, the identity migration) keep a shrink-only row each.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

NAME = "r" + "ob"  # spelled apart so this file does not count itself

SCOPE = (
    "agents", "api", "cli", "core", "modules", "surfaces", "tools", "utils", "webview",
    "cron", "migrations", "assets", "datagen", "tests", "packs", "polyrob", "devtools",
    "data/characters", "data/prompts", "docs/guide", "docs/CONFIGURATION.md",
    "docs/SKILL_AUTHORING_STANDARD.md", "docs/comparison.md", "docs/examples.md",
    "README.md", "CONTRIBUTING.md", "SECURITY.md", "TRADEMARK.md", "RELEASING.md",
    "pyproject.toml", "main.py", "install.sh", "Dockerfile", "docker-compose.yml",
    "conftest.py", "config", ".github",
)

#: The name as a word in any case (``polyrob`` and ``robinhood`` are not words of it),
#: or as an identifier prefix (``<name>_usage_summary``).
HIT = re.compile(rf"(?i)\b{NAME}\b|\b{NAME}_")

#: The legacy home and its secret-guard rows: ``~/.<name>``, ``.<name>/.env``,
#: ``/etc/<name>``. Stripped before the match (functional back-compat).
LEGACY_HOME = re.compile(rf"(?i)(?<![\w-])\.{NAME}\b|/etc/{NAME}\b")

#: Lines that keep the old name because live data or an old install depends on it.
#: Shrink-only: lower a row when it falls, never raise one, never add one for copy.
CEILING = {
    # The one-time identity/<name> -> identity/polyrob copy and its tests.
    "core/home_migration.py": 3,
    "core/instance.py": 1,
    "polyrob/app/bootstrap.py": 1,
    "tests/conftest.py": 3,
    "tests/unit/core/test_home_migration.py": 6,
    # API keys issued before the polyrob_ prefix still authenticate and are redacted.
    "api/api_key_auth.py": 1,
    "core/secret_patterns.py": 1,
    "core/security_logging_filter.py": 4,
    "tests/unit/core/test_security_logging_filter.py": 1,
    # A stale install can still have the old admin binary; the guard keeps blocking it.
    "core/security/command_guard.py": 2,
    "tests/unit/core/security/test_command_guard.py": 2,
    # A CREATE2 address measured against the live factory with this token name.
    "tests/unit/core/wallet/test_deploy_guard.py": 1,
    # The private repository's name, which the public export must never contain.
    "tests/test_public_release_surface.py": 1,
}


def _hits():
    files = subprocess.run(
        ["git", "ls-files", "--", *SCOPE], cwd=ROOT, capture_output=True, text=True,
        check=True,
    ).stdout.split()
    counts = {}
    for rel in files:
        if rel == "tests/test_no_instance_name_ratchet.py" or Path(rel).name.startswith("CHANGELOG"):
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        n = sum(1 for line in text.splitlines() if HIT.search(LEGACY_HOME.sub("", line)))
        if n:
            counts[rel] = n
    return counts


def test_the_shipped_tree_names_no_instance():
    over = {rel: n for rel, n in _hits().items() if n > CEILING.get(rel, 0)}
    assert not over, (
        "The shipped tree names an instance (owner copy says 'the agent'; a fixture "
        f"uses a neutral name): {over}")


def test_ceilings_only_fall():
    counts = _hits()
    stale = {rel: (n, counts.get(rel, 0)) for rel, n in CEILING.items()
             if counts.get(rel, 0) < n}
    assert not stale, f"lower these rows to the live count: {stale}"
