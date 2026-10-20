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
``webview/server.py`` (a ``"data" not in entry`` dict-key check). ``core/runtime_paths.py``'s
``_LEGACY_SESSIONS_DEFAULT`` and the remaining loudly-logged legacy
return are deliberate. ``core/config.py`` Field defaults are anchored to the data home by
bootstrap; ``polyrob/app/bootstrap.py``'s DATA_ROOT path_manager default and the messages/
telemetry DB axis are location MOVES needing a migration (deferred, WS-3 notes).
"""
from pathlib import Path

from tests.support.repo_index import repo_index

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

ROOTS = ("core", "polyrob", "agents", "tools", "modules", "cli", "api", "cron",
         "surfaces", "webview", "utils", "scripts")

# file -> max allowed violating lines. SHRINK-ONLY: lower/delete rows as sites get fixed.
BASELINE = {
    "api/app.py": 1,
    # cli/commands/owner.py: row DROPPED 2026-09-08 — its last violation (a
    # docstring mention of the OLD behaviour) went with the admin_data_home fix.
    # cli/commands/* + persona/h_self swept through data_dir_or_home() 2026-07-16;
    # the data/characters mirror moved to persona_resolver.character_search_dirs
    # (2026-08-20). cli/ui/commands/handlers.py: row DROPPED 2026-09-09 — its
    # last violation was /persona's bio column reading a cwd-relative
    # Path("data")/"characters", which resolved only from the repo root and was
    # blank for every row in a real profile (F3); it now reads
    # character_search_dirs(), the same union the names come from.
    # polyrob/app/bootstrap.py: row DROPPED 2026-10-10 (W8) — the server
    # path_manager default is resolve_session_data_root(); api/kb/endpoints.py:
    # row DROPPED (W8) — the KB opens the agent's <data_home>/memory.db.
    # core/config.py: row DROPPED 2026-10-13 (080 WS-1) — data_dir/db_path
    # default to the data home; no relative "data/" Field default is left.
    "core/credit_sentinel.py": 1,     # T3-fixed; "data" only in the last-resort except
    "core/runtime_paths.py": 1,       # _LEGACY_SESSIONS_DEFAULT (deliberate terminal fallback)
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
            for line in repo_index().text(py, errors="replace").splitlines():
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


# --- W8 (2026-10-10): no new open-coded "<name>.db" joins ----------------------
# A store's path is ONE helper: core.runtime_paths.store_db_path(name, dir) (or a
# named one: goals_db_path, cron_db_path, correspondents_db_path,
# surfaces_db_path, session_registry_db_path, memory_db_path), keyed by the
# core.db_manifest registry. The rows below are the remaining joins (mostly the
# owning store module's own default path) — SHRINK-ONLY like BASELINE above.
import re as _re

_DB_LITERAL = _re.compile(r"""["'][a-z0-9_]+\.(?:db|sqlite3?)["']""")
_DB_JOIN = _re.compile(
    r"""os\.path\.join\(|joinpath\(|\)\s*/\s*["']|\w\s*/\s*["'][a-z0-9_]+\.(?:db|sqlite)""")
_DB_JOIN_ROOTS = ROOTS[:-1] + ("packs",)   # scripts/ are operator tools
_DB_JOIN_ALLOWED = {"core/runtime_paths.py", "core/db_manifest.py"}

DB_JOIN_BASELINE = {
    "cli/commands/doctor.py": 1,
    "cli/commands/kb.py": 1,
    "cli/commands/keys.py": 1,
    "cli/commands/owner.py": 2,
    "cli/commands/surface.py": 2,
    "core/autonomy_control.py": 1,
    "core/event_log.py": 1,
    "core/pairing.py": 1,
    "core/payments/assets.py": 1,
    "core/session_control.py": 1,
    "core/session_retention.py": 1,
    "core/signer/shadow.py": 1,
    "core/signer/store.py": 1,
    "core/status_delivery.py": 1,
    "core/status_economics.py": 1,
    "core/status_room_actions.py": 1,
    "core/status_rooms.py": 2,
    "core/surfaces/access.py": 2,
    "core/surfaces/bootstrap.py": 4,
    "core/surfaces/group_admin.py": 1,
    "core/surfaces/ledger_ingest.py": 1,
    "core/surfaces/room_action_store.py": 1,
    "core/wallet/bridge_guard.py": 1,
    "core/wallet/submission_journal.py": 1,
    "core/wallet/token_pins.py": 2,
    "core/wallet/token_provenance.py": 2,
    "cron/wake_gate.py": 1,
    "modules/database/database_manager.py": 1,
    "modules/skills/skill_usage.py": 2,
    "packs/x/polyrob_x/surface/harness.py": 1,
    "polyrob/app/autonomy_runtime.py": 2,
    "surfaces/dingtalk/harness.py": 1,
    "surfaces/discord/harness.py": 1,
    "surfaces/email/harness.py": 2,
    "surfaces/feishu/harness.py": 2,
    "surfaces/signal/harness.py": 1,
    "surfaces/slack/harness.py": 1,
    "surfaces/telegram/contacts_ops.py": 1,
    "surfaces/telegram/harness.py": 2,
    "surfaces/whatsapp/harness.py": 2,
    "tools/controller/approval_queue.py": 3,
    "webview/activity.py": 1,
}


def _scan_db_joins():
    counts = {}
    for root in _DB_JOIN_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            rel = py.relative_to(REPO).as_posix()
            if "/tests/" in rel or rel in _DB_JOIN_ALLOWED:
                continue
            n = 0
            for line in repo_index().text(py, errors="replace").splitlines():
                if line.strip().startswith("#"):
                    continue
                if _DB_LITERAL.search(line) and _DB_JOIN.search(line):
                    n += 1
            if n:
                counts[rel] = n
    return counts


def test_no_new_db_name_joins():
    counts = _scan_db_joins()
    over = {f: n for f, n in counts.items() if n > DB_JOIN_BASELINE.get(f, 0)}
    assert not over, (
        "New open-coded \"<name>.db\" path join(s) — use "
        "core.runtime_paths.store_db_path(name, data_dir) or a named helper "
        "(goals_db_path/cron_db_path/correspondents_db_path/surfaces_db_path/"
        "session_registry_db_path/memory_db_path), and register a new store in "
        "core.db_manifest.DB_REGISTRY:\n"
        + "\n".join(f"  {f}: {n} > baseline {DB_JOIN_BASELINE.get(f, 0)}"
                    for f, n in sorted(over.items())))


def test_db_join_baseline_rows_still_needed():
    counts = _scan_db_joins()
    stale = {f: b for f, b in DB_JOIN_BASELINE.items() if counts.get(f, 0) < b}
    assert not stale, (
        "DB-join baseline rows exceed current joins — tighten them (shrink-only):\n"
        + "\n".join(f"  {f}: baseline {b} > actual {counts.get(f, 0)}"
                    for f, b in sorted(stale.items())))
