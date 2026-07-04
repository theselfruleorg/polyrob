"""Grep-guard: no stray bare-``ROB_`` framework env flag survives the polyrob rename.

The framework rename (doc 02) is a clean break: every framework env flag is
``POLYROB_*``. This guard greps the source tree for ``ROB_[A-Z]`` and asserts every
hit is a known-legitimate non-flag token (the allowlist), so a NEW bare framework
``ROB_`` env flag fails CI instead of silently splitting the config surface.

Scans ``*.md`` too (not just code): AGENTS.md, docs/CONFIGURATION.md, and a shipped
SKILL.md all carried stale bare-``ROB_LOCAL``/``ROB_TOOL_DENYLIST``/etc. mentions that
the original code-only guard never caught (2026-07-01 review) — docs are exactly where
this bug class hides.

Legitimate (allowlisted) non-flag uses:
  - ``POLYROB_*`` — every renamed framework flag.
  - ``ROB_LOCAL`` — the one intentionally-kept deprecated alias (``is_local()`` still
    accepts it); comments/docs naming it to explain the alias are legitimate.
  - ``ROB_TO_A2A_STATE`` / ``A2A_TO_ROB_STATE`` — a2a module-global dicts, not env vars.
  - ``CLI_TODO_DOT_ROB`` — a project-dir-name toggle (token contains ROB, not a ROB_ flag).
  - ``_ROB_KEY_RE`` — the instance ``rob_`` API-key redaction regex (instance-level).
  - ``_ROB_VERSION`` — a CLI version import alias.
  - ``ROB_TEST_`` / ``ROB_CORE_SERVER_SPLIT_SPEC`` / ``REFERENCE_VS_ROB_CONTEXT`` /
    ``HERMES_VS_ROB_REVIEW`` — test sentinels / doc filenames in comments.

CHANGELOG.md is excluded entirely: it's dated, point-in-time history (AGENTS.md's own
doc map marks it "Append-only", not a living reference), so it's expected and correct
for it to name a flag by whatever it was actually called at that point in time.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# A line passes if it contains ANY of these substrings.
ALLOWLIST = (
    "POLYROB_",
    # ROB_LOCAL is the ONE intentionally-kept deprecated alias — is_local() reads
    # `_bool_env("POLYROB_LOCAL") or _bool_env("ROB_LOCAL")` (agents/task/constants.py),
    # so comments/docs that name it (to explain the alias) are legitimate, not stray.
    "ROB_LOCAL",
    "ROB_TO_A2A_STATE",
    "A2A_TO_ROB_STATE",
    "ROB_TEST_",
    "ROB_CORE_SERVER_SPLIT_SPEC",
    "CLI_TODO_DOT_ROB",
    "REFERENCE_VS_ROB_CONTEXT",
    "HERMES_VS_ROB_REVIEW",
    "_ROB_VERSION",
    "_ROB_KEY_RE",
)

# This guard test itself names the forbidden pattern; exclude it from its own scan.
THIS_FILE = "tests/unit/test_no_stray_rob_env.py"

# Dated/historical, not a living reference — see module docstring.
CHANGELOG_FILE = "CHANGELOG.md"


def _grep_hits():
    proc = subprocess.run(
        ["git", "grep", "-nE", r"ROB_[A-Z]", "--",
         "*.py", "*.sh", "*.service", "*.conf", "*.toml", "*.md"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    # git grep exits 1 when there are no matches — that's fine (empty output).
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    hits = []
    for ln in lines:
        # format: path:lineno:content
        parts = ln.split(":", 2)
        if len(parts) < 3:
            continue
        path, _lineno, content = parts
        if path.startswith("tests/") or path in (THIS_FILE, CHANGELOG_FILE):
            continue
        hits.append((path, content))
    return hits


def test_no_stray_rob_env_flags():
    offenders = [
        f"{path}: {content.strip()}"
        for path, content in _grep_hits()
        if not any(tok in content for tok in ALLOWLIST)
    ]
    assert not offenders, (
        "Stray bare-ROB_ framework token(s) found (rename to POLYROB_ or allowlist "
        "if a legitimate non-flag use):\n" + "\n".join(offenders)
    )
