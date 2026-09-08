"""Reverse flags contract (030 WS-F2): env vars the CODE reads ⊆ the catalog.

The forward contract (``test_flags.py``) asserts every documented flag row is in
``core.flags.REGISTRY``. This is the missing direction: scan the shipped source
tree for env-var READS and assert every collected name is (a) a registry flag,
(b) matched by a dynamic ``<...>`` catalog pattern, or (c) on the small explicit
allowlist of genuine non-config vars below. A new ``os.getenv("MY_FLAG")`` in
shipped code fails CI until ``docs/CONFIGURATION.md`` gains a row (then
``python scripts/gen_flags_catalog.py`` + ``python scripts/gen_user_guide_refs.py``).

Scanner scope is the committed source dirs only (never tests/scripts/docs), via
``git ls-files`` where available so another session's in-flight uncommitted
files on the shared working tree can never flap this test.
"""
import re
import subprocess
from pathlib import Path

from core.flags import REGISTRY, is_secret_flag, pattern_flag_for

REPO_ROOT = Path(__file__).resolve().parents[3]

# Shipped source dirs — deliberately excludes tests/, scripts/, docs/, avatar/,
# deployment/, migrations/ (not agent-runtime config surface).
SOURCE_DIRS = (
    "core", "modules", "tools", "agents", "cli", "webview",
    "surfaces", "cron", "api", "utils",
)

# The env-read idioms POLYROB uses. Assignment writes (os.environ["X"] = ...)
# are excluded by the negative lookahead ("==" comparisons still count as reads).
READ_RE = re.compile(
    r"""(?:
        os\.getenv\(\s*['"](?P<g1>[A-Za-z_][A-Za-z0-9_]*)['"]
      | os\.environ\.get\(\s*['"](?P<g2>[A-Za-z_][A-Za-z0-9_]*)['"]
      | os\.environ\[\s*['"](?P<g3>[A-Za-z_][A-Za-z0-9_]*)['"]\s*\](?!\s*=[^=])
      | \b_?(?:bool|int|float)_env\(\s*['"](?P<g4>[A-Za-z_][A-Za-z0-9_]*)['"]
    )""",
    re.VERBOSE,
)

# Config-shaped names only (drops lowercase locals and 1-2 char names).
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

# Genuine non-config env vars — OS/terminal/runtime facts the process observes,
# never POLYROB configuration. Keep SMALL; every entry needs a one-phrase
# justification, and a stale entry (no read left in the tree) fails the
# hygiene test below.
ALLOWLIST = {
    "DISPLAY",           # X11 display presence (headed-browser / signup ceremony detection)
    "NO_COLOR",          # no-color.org terminal convention
    "PATH",              # OS executable search path (wrapper-install check)
    "PYTHONPATH",        # Python runtime path, passed through to the webview child process
    "TERM",              # terminal type (title/theme capability detection)
    "XDG_SESSION_TYPE",  # Wayland/X11 session detection for browser flags
}


def _source_files():
    """Committed .py files under SOURCE_DIRS (git ls-files; rglob fallback).

    Any path with a tests/test/__pycache__ segment is skipped either way.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--", *SOURCE_DIRS],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0 and out.stdout.strip():
            files = [REPO_ROOT / line for line in out.stdout.splitlines()
                     if line.endswith(".py")]
        else:
            raise OSError("git ls-files unavailable")
    except Exception:
        files = [p for d in SOURCE_DIRS if (REPO_ROOT / d).is_dir()
                 for p in (REPO_ROOT / d).rglob("*.py")]
    return [
        f for f in files
        if f.is_file() and not any(
            part in ("tests", "test", "__pycache__")
            for part in f.relative_to(REPO_ROOT).parts
        )
    ]


def _scan_env_reads() -> dict:
    """name -> sorted list of 'relpath:line' read sites."""
    hits: dict = {}
    for py in _source_files():
        rel = py.relative_to(REPO_ROOT)
        try:
            text = py.read_text()
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.match(r"\s*del\s", line):
                continue  # deletion, not a read
            for m in READ_RE.finditer(line):
                name = next(g for g in m.groups() if g)
                if NAME_RE.match(name):
                    hits.setdefault(name, []).append(f"{rel}:{lineno}")
    return hits


def test_every_env_read_is_cataloged():
    hits = _scan_env_reads()
    assert len(hits) > 300, (
        f"scanner found only {len(hits)} env reads — scanner or tree layout broke"
    )
    missing = {
        name: sites[:3]
        for name, sites in sorted(hits.items())
        if name not in REGISTRY
        and pattern_flag_for(name) is None
        and name not in ALLOWLIST
    }
    assert not missing, (
        f"{len(missing)} env var(s) read by shipped code but absent from the flag "
        "catalog. Add a row to docs/CONFIGURATION.md (real default from the code "
        "anchor), then run BOTH `python scripts/gen_flags_catalog.py` AND "
        "`python scripts/gen_user_guide_refs.py` — or delete the dead read, or "
        f"(genuine OS/runtime var only) extend the allowlist here: {missing}"
    )


def test_allowlist_stays_tight():
    """Every allowlist entry must still be read somewhere and must not be a
    registry flag (a documented flag on the allowlist would silently disable
    the contract for it)."""
    hits = _scan_env_reads()
    stale = ALLOWLIST - set(hits)
    assert not stale, f"allowlist entries with no remaining read in the tree: {sorted(stale)}"
    shadowing = ALLOWLIST & set(REGISTRY)
    assert not shadowing, f"allowlist entries that are documented flags: {sorted(shadowing)}"


def test_newly_cataloged_secrets_are_masked():
    # The WS-F2 debt payment added rows for real credentials; each must mask.
    for name in (
        "AGENT_WALLET_MASTER_SEED", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
        "DISCORD_BOT_TOKEN", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "ADMIN_TOKEN", "API_SECRET",
        "GITHUB_TOKEN", "GH_TOKEN", "ALCHEMY_API_KEY", "POSTHOG_API_KEY",
        "CDP_API_KEY_SECRET", "TWITTER_API_KEY", "TWITTER_API_SECRET_KEY",
        "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET",
    ):
        assert is_secret_flag(name), f"{name} would print UNMASKED in doctor --flags"


def test_owner_surfaces_alias_registered():
    # Both the canonical and legacy spellings resolve (030 WS-B1 rail config).
    assert "OWNER_SURFACE" in REGISTRY
    assert "OWNER_SURFACES" in REGISTRY


def test_enum_flags_consult_enum_ssot_for_kind():
    """E9: enum-shaped flags take kind from core.config_policy.flag_enums, so a
    numeric-string enum default (AGENT_COMPUTE_POSTURE's `0`, a 0-3 ladder) can
    never be mis-kinded bool/int by the textual default heuristic."""
    from core.config_policy.flag_enums import FLAG_ENUMS
    from core.flags import resolve_flag

    for name in FLAG_ENUMS:
        assert name in REGISTRY, f"enum SSOT names undocumented flag {name}"
        assert REGISTRY[name].kind == "enum", (
            f"{name} kinded {REGISTRY[name].kind!r}, not 'enum'"
        )
    # The concrete landmine: posture 2 must resolve as the string "2" — never
    # a bool True, never an int display surprise.
    r = resolve_flag("AGENT_COMPUTE_POSTURE", {"AGENT_COMPUTE_POSTURE": "2"})
    assert r.value == "2" and r.value is not True
    r = resolve_flag("AGENT_COMPUTE_POSTURE", {})
    assert r.value == "0"  # bare token from the doc cell, not "`0`"
    r = resolve_flag("AUTONOMY_MODE", {})
    assert r.value == "supervised"
