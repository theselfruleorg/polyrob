"""Grep-guard: no long-hex signing secret is hardcoded in a git-TRACKED file.

A committed ``JWT_SECRET_KEY=<64-hex>`` (as once shipped in the systemd units) lets
anyone deploying those files verbatim run with a publicly-known signing key and forge
auth tokens against that instance. This guard greps the tracked tree for any assignment
of a 32+ byte hex value to a secret-shaped key and fails CI if one reappears, so the
class of leak that A1 removed can't silently regress.

Scope: tracked files only (``git grep`` == the index), the exact set that ships. It
targets *assignments* to secret-named keys — not arbitrary hex (git SHAs, test vectors)
— so it stays specific. Placeholder values (``CHANGE_ME``, ``REPLACE_WITH_*``,
``openssl rand``) are fine by construction (they aren't hex). CHANGELOG.md is excluded
as dated history.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# key = 32+ byte (64+ hex char) value assigned to a secret-shaped env/name.
SECRET_KEYS = ("JWT_SECRET_KEY", "PAYMENT_MASTER_SEED", "SECRET_KEY", "MASTER_SEED")
_HEX_ASSIGN = re.compile(
    r"(?:%s)\s*=\s*[\"']?[0-9a-fA-F]{64,}" % "|".join(SECRET_KEYS)
)

THIS_FILE = "tests/unit/test_no_tracked_signing_keys.py"
CHANGELOG_FILE = "CHANGELOG.md"


def _tracked_lines():
    proc = subprocess.run(
        ["git", "grep", "-nE",
         r"(JWT_SECRET_KEY|PAYMENT_MASTER_SEED|SECRET_KEY|MASTER_SEED)[ ]*=[ ]*['\"]?[0-9a-fA-F]{64}",
         "--", "*.py", "*.sh", "*.service", "*.conf", "*.toml", "*.md", "*.env*", "*.yml", "*.yaml"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    # git grep exits 1 with no matches — empty output is the pass case.
    out = []
    for ln in proc.stdout.splitlines():
        if not ln.strip():
            continue
        parts = ln.split(":", 2)
        if len(parts) < 3:
            continue
        path, _lineno, content = parts
        if path in (THIS_FILE, CHANGELOG_FILE):
            continue
        out.append((path, content))
    return out


def test_no_hardcoded_signing_secret_in_tracked_files():
    offenders = [
        f"{path}: {content.strip()}"
        for path, content in _tracked_lines()
        if _HEX_ASSIGN.search(content)
    ]
    assert not offenders, (
        "Hardcoded long-hex signing secret found in a tracked file. Remove it and "
        "source the secret from an EnvironmentFile / generate per-deployment "
        "(openssl rand -hex 32):\n" + "\n".join(offenders)
    )
