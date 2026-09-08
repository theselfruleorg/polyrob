"""ops_digest: assemble the COMMS-structured periodic owner update from real state.

Pure assembly/format functions are tested here; the I/O collectors are thin.
"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "ops_digest",
    Path(__file__).resolve().parents[3] / "scripts" / "ops_digest.py",
)
ops_digest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ops_digest)


_FULL = {
    "date": "2026-08-24",
    "fixes": ["deploy verify false-rollback (pipefail/SIGPIPE)", "x402 approve lane"],
    "proposals": [("028 exit-side allowance exemption", "unblock treasury approves")],
    "release": {"scope": ["profiles", "neutral identity"], "status": "published v0.12.0"},
    "goals": {"done": 5, "blocked": 1, "treasury_usd": "10.00", "budget": "z.ai plan"},
    "asks": ["Rotate the PyPI token (pasted in chat)"],
}


def test_assemble_has_all_sections():
    md = ops_digest.assemble_report(_FULL)
    for h in ("Fixes implemented", "Feature proposals", "New release",
              "Goals & money", "Asks"):
        assert h in md
    assert "- deploy verify false-rollback" in md
    assert "028 exit-side allowance exemption" in md
    assert "published v0.12.0" in md
    assert md.startswith("# ") or md.startswith("Update")


def test_assemble_omits_empty_sections():
    md = ops_digest.assemble_report({"date": "2026-08-24", "fixes": [],
                                     "proposals": [], "release": {}, "goals": {},
                                     "asks": []})
    assert "Feature proposals" not in md
    assert "Asks" not in md
    # A quiet day still produces a valid, non-empty report.
    assert "2026-08-24" in md


def test_headline_summarizes_counts():
    line = ops_digest.format_headline(_FULL)
    assert "2 fix" in line and "1 proposal" in line
    assert len(line) < 200


def test_parse_git_log_filters_to_fix_feat():
    lines = [
        "abc123 fix(deploy): verify grep -c",
        "def456 feat(ops): self-sufficient alerts",
        "ghi789 docs(ops): tick log 12:00",
        "jkl012 chore: bump",
    ]
    fixes = ops_digest.parse_git_fixes(lines)
    assert any("verify grep" in f for f in fixes)
    assert any("self-sufficient alerts" in f for f in fixes)
    assert not any("tick log" in f for f in fixes)  # docs/chore excluded


def test_release_version_from_changelog_head():
    # The box clone may lack the newest git tag (tags don't ride the branch
    # bridge), so the version comes from the CHANGELOG head, which is always in
    # the tree.
    changelog = "# Changelog\n\n## [Unreleased]\n\n## [0.12.0] — 2026-08-21\n- x\n"
    assert ops_digest.changelog_version(changelog) == "0.12.0"
    assert ops_digest.changelog_version("no headings here") == ""


def test_parse_proposal_title_and_status():
    body = "# 028 — Exit-side allowance exemption\n\n**Status:** PROPOSED\n\nbody"
    title, status = ops_digest.parse_proposal(body)
    assert "Exit-side allowance" in title
    assert status.lower().startswith("proposed")


def _write_backlog(tmp_path, section_body):
    repo = tmp_path / "repo"
    (repo / "docs" / "ops").mkdir(parents=True)
    (repo / "docs" / "ops" / "rob-backlog.md").write_text(
        "# Rob #1 — operations backlog\n\n## Open owner asks\n\n"
        + section_body
        + "\n\n## Tick log\n\nsomething else entirely\n"
    )
    return str(repo)


def test_collect_asks_returns_a_live_ask(tmp_path):
    repo = _write_backlog(tmp_path, "- [2026-08-27] **A live ask** — details here. (open)\n")
    asks = ops_digest.collect_asks(repo)
    assert asks == ["A live ask"]


def test_collect_asks_skips_a_bracket_fixed_block(tmp_path):
    """2026-08-27 finding: a bullet resolved INLINE (this file's own
    convention — append the update to the same bullet rather than deleting
    it) kept surfacing in every daily digest because the extractor only read
    the bolded title, never the resolution text later in the same block."""
    repo = _write_backlog(tmp_path, (
        "- [2026-08-21] **Twitter client resolves to `None`** — investigating.\n"
        "  **[FIXED 2026-08-21 06:34Z]** root-caused and patched. (closed)\n"
    ))
    assert ops_digest.collect_asks(repo) == []


def test_collect_asks_skips_dated_resolved_block(tmp_path):
    repo = _write_backlog(tmp_path, (
        "- [2026-08-21] **All LLM providers exhausted** — critical, no fallback.\n"
        "  **RESOLVED 2026-08-25 18:01:49Z:** self-resolved at reset. (closed)\n"
    ))
    assert ops_digest.collect_asks(repo) == []


def test_collect_asks_skips_shipped_and_wontfix_tags(tmp_path):
    repo = _write_backlog(tmp_path, (
        "- [2026-08-01] **Ship the thing** — done now. (shipped)\n"
        "- [2026-08-02] **Won't do this** — decided against it. (wontfix)\n"
        "- [2026-08-03] **Still needed** — genuinely open. (open)\n"
    ))
    assert ops_digest.collect_asks(repo) == ["Still needed"]


def _make_goals_db(tmp_path, rows):
    import sqlite3
    db = tmp_path / "goals.db"
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE goals (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'goal',
        status TEXT NOT NULL DEFAULT 'ready', priority INTEGER NOT NULL DEFAULT 5,
        parent_id TEXT, payload TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL)""")
    for i, (title, kind, status, payload) in enumerate(rows):
        con.execute(
            "INSERT INTO goals (id, user_id, title, kind, status, payload, created_at) "
            "VALUES (?, 'rob', ?, ?, ?, ?, ?)",
            (f"id{i}", title, kind, status, payload, float(i)))
    con.commit()
    con.close()
    return str(tmp_path)


def test_collect_live_asks_returns_open_asks_from_the_goal_board(tmp_path):
    """2026-08-28 live bug: the digest's ONLY ask source was a hand-maintained
    markdown section that drifted — a resolved ask (Solana RPC, pinned weeks
    earlier) still showed as open, and two genuinely current asks (already
    tracked via `polyrob owner asks`) never appeared at all. This reads the
    same live rows the CLI does."""
    data_dir = _make_goals_db(tmp_path, [
        ("Grant defi_trade on the treasury cycle", "ask", "open", "{}"),
        ("Deploy an x402 endpoint", "ask", "open", "{}"),
        ("Old resolved ask", "ask", "fulfilled", "{}"),
        ("Not an ask at all", "goal", "open", "{}"),
    ])
    asks = ops_digest.collect_live_asks(data_dir)
    assert asks == ["Grant defi_trade on the treasury cycle", "Deploy an x402 endpoint"]


def test_collect_live_asks_excludes_tool_approval_asks(tmp_path):
    """Tool-approval asks have their OWN surface (`owner pending`) — excluded
    here so one isn't shown twice under two different id shapes, matching
    cli/commands/owner.py::asks."""
    data_dir = _make_goals_db(tmp_path, [
        ("Approve this tool", "ask", "open", '{"ask_kind": "tool_approval"}'),
        ("A real ask", "ask", "open", "{}"),
    ])
    assert ops_digest.collect_live_asks(data_dir) == ["A real ask"]


def test_collect_live_asks_missing_db_returns_empty(tmp_path):
    assert ops_digest.collect_live_asks(str(tmp_path)) == []


def test_merge_asks_prefers_live_and_dedupes_overlap():
    merged = ops_digest._merge_asks(
        ["Grant defi_trade on the treasury cycle"],
        ["Grant defi_trade", "A genuinely separate markdown-only ask"])
    assert merged == [
        "Grant defi_trade on the treasury cycle",
        "A genuinely separate markdown-only ask",
    ]
