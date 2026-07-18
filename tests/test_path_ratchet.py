"""Path-handling ratchet (WS-3, 2026-07-16).

Freezes the remaining relative-``"data"`` path constructions (a latent CWD/install-tree
write whenever the container/config is absent) and forbids NEW ones. The fix for a hit is
``core.runtime_paths.data_dir_or_home()`` / ``goals_db_path()`` / ``cron_db_path()`` /
``resolve_data_home()`` / ``resolve_session_data_root()`` — never a fresh ``or "data"``
fallback or a relative ``data/...`` literal.

Mechanism: per-file baseline counts that may only SHRINK. When you fix a site, lower (or
delete) its row here so the ratchet tightens. Adding a violation to a clean file — or
raising a file's count — fails.

Known baked-in non-violations kept in the baseline rather than special-cased:
``webview/server.py`` (a ``"data" not in entry`` dict-key check) and ``cli/commands/
owner.py`` (a docstring mentioning the OLD behaviour). ``core/runtime_paths.py``'s
``_LEGACY_SESSIONS_DEFAULT`` and ``core/wallet/audit_sink.py``'s loudly-logged legacy
return are deliberate. ``core/config.py`` Field defaults are anchored to the data home by
bootstrap; ``core/bootstrap.py``'s DATA_ROOT path_manager default and the messages/
telemetry DB axis are location MOVES needing a migration (deferred, WS-3 notes).
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PATTERNS = (
    'os.path.join("data"',
    'Path("data")',
    '"./data',
    'default="data/',
    'or "data"',
    '"data_dir", "data"',
    # Single-quote variants — action_registration.py used them, invisible to the
    # double-quote-only patterns above until the 2026-07-16 completion pass.
    "os.path.join('data'",
    "Path('data')",
    "'./data",
    "default='data/",
    "or 'data'",
    "'data_dir', 'data'",
)

ROOTS = ("core", "agents", "tools", "modules", "cli", "api", "cron",
         "surfaces", "webview", "utils", "scripts")

# file -> max allowed violating lines. SHRINK-ONLY: lower/delete rows as sites get fixed.
BASELINE = {
    "api/app.py": 1,
    "api/kb/endpoints.py": 1,
    "cli/commands/owner.py": 1,       # docstring mention of the OLD behaviour
    # cli/commands/* + persona/h_self swept through data_dir_or_home() 2026-07-16;
    # handlers.py keeps 2: the data/characters shipped-tree mirror (CharacterManager
    # convention, read-only), not a data-home fallback.
    "cli/ui/commands/handlers.py": 2,
    "core/bootstrap.py": 1,           # DATA_ROOT path_manager default (location move, deferred)
    "core/config.py": 2,              # Field defaults, anchored by bootstrap
    "core/credit_sentinel.py": 1,     # T3-fixed; "data" only in the last-resort except
    "core/runtime_paths.py": 1,       # _LEGACY_SESSIONS_DEFAULT (deliberate terminal fallback)
    "core/wallet/audit_sink.py": 1,   # loud legacy return on the money path (deliberate)
    "webview/repair_sessions.py": 1,
    "webview/server.py": 1,           # false positive: '"data" not in entry'
}


def _scan():
    counts = {}
    for root in ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            rel = py.relative_to(REPO).as_posix()
            if "/tests/" in rel:
                continue
            n = 0
            for line in py.read_text(errors="replace").splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if any(pat in line for pat in PATTERNS):
                    n += 1
            if n:
                counts[rel] = n
    return counts


def test_no_new_relative_data_paths():
    counts = _scan()
    over = {f: n for f, n in counts.items() if n > BASELINE.get(f, 0)}
    assert not over, (
        "New relative-\"data\" path construction(s) — route through "
        "core.runtime_paths (data_dir_or_home/goals_db_path/cron_db_path/"
        "resolve_data_home/resolve_session_data_root) instead:\n"
        + "\n".join(f"  {f}: {n} > baseline {BASELINE.get(f, 0)}" for f, n in sorted(over.items()))
    )


def test_baseline_rows_still_needed():
    """A fixed file must have its row lowered/removed so the ratchet actually tightens."""
    counts = _scan()
    stale = {f: b for f, b in BASELINE.items() if counts.get(f, 0) < b}
    assert not stale, (
        "Baseline rows exceed current violations — tighten them (shrink-only ratchet):\n"
        + "\n".join(f"  {f}: baseline {b} > actual {counts.get(f, 0)}" for f, b in sorted(stale.items()))
    )
