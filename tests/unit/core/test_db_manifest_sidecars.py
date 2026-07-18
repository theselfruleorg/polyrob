"""SIDECAR_DB_NAMES must cover every sidecar sqlite DB the surface/deploy layers open.

Two generations of this bug (D11 2026-07-11, T1 2026-07-16) were caused by adding a
new sidecar store without registering it in core/db_manifest.py — `polyrob update`
backup/rollback then silently skips the file (data loss / stale-dedup replay on
rollback). The grep-based completeness check makes the third generation fail loudly.
"""
import re
from pathlib import Path

from core.db_manifest import SIDECAR_DB_NAMES, candidate_sqlite_dbs

REPO = Path(__file__).resolve().parents[3]

EXPECTED_NEW = {
    "slack_dedup.db", "signal_dedup.db", "discord_dedup.db", "x_dedup.db",
    "wa_window.db", "group_allowlist.db", "conversations.db", "outbox.db",
    "surface_state.db", "deployed_apps.db",
}


def test_new_surface_sidecars_registered():
    missing = EXPECTED_NEW - set(SIDECAR_DB_NAMES)
    assert missing == set(), f"sidecar DBs missing from manifest: {sorted(missing)}"


def test_candidates_include_new_sidecars(tmp_path):
    names = {p.name for p in candidate_sqlite_dbs(tmp_path)}
    assert EXPECTED_NEW <= names


_DB_LITERAL = re.compile(
    r"""os\.path\.join\(\s*data_dir[^,]*,\s*["']([a-z0-9_]+\.db)["']""")


def test_grep_completeness_surfaces_and_hf_deploy():
    """Every ``os.path.join(data_dir, "<x>.db")`` under surfaces/, core/surfaces/,
    tools/hf_deploy/ must be in the manifest — the exact pattern that produced
    both prior generations of this bug."""
    found = set()
    for sub in ("surfaces", "core/surfaces", "tools/hf_deploy"):
        for py in (REPO / sub).rglob("*.py"):
            found |= set(_DB_LITERAL.findall(
                py.read_text(encoding="utf-8", errors="replace")))
    unregistered = found - set(SIDECAR_DB_NAMES)
    assert unregistered == set(), (
        f"sidecar DBs opened but not in manifest: {sorted(unregistered)}")
