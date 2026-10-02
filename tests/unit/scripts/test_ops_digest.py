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


def test_parse_git_log_keeps_module_scoped_changes_and_drops_the_noise_scopes():
    """2026-09-22: the five fixes deployed at 05:09Z were committed as
    `cron:`, `status:`, `defi:`, `wallet:` and `docs:` lines — none of them
    `fix|feat|perf` — and the morning digest's "Fixes implemented" listed
    yesterday's release note instead. A `<scope>: <subject>` line is a real
    change unless the scope is a known noise word (docs/chore/tests/intel/
    handoff/ci/style/wip/revert/merge)."""
    lines = [
        "72ec91955 cron: rail preflight — a $0 skip when the precondition is already false",
        "a9d5cc741 wallet: one owner line per settled transaction",
        "058: align every dependent of the lean base",
        "18a659b78 docs: regenerate the user-guide configuration reference",
        "ef861daec tests(gemini): skip, not error, when the extra is absent",
        "d4f4c5060 intel: scorecard $ cell rounds to cents",
        "dad6d19dc handoff: validate the completeness of 058",
        "9b1c78a04 docs(tick 290): recent-notices build",
        "0000000 chore(deps): bump",
        "1111111 Merge branch 'x'",
    ]
    fixes = ops_digest.parse_git_fixes(lines)
    assert fixes == [
        "rail preflight — a $0 skip when the precondition is already false",
        "one owner line per settled transaction",
        "align every dependent of the lean base",
    ]


def test_a_filed_observation_or_a_retraction_is_not_a_fix():
    """⚠️ 2026-09-24 05:20Z, caught 2h40m before the digest fired.

    This loop files findings as commits under `inbox:` and `correction:` — 13
    `inbox:` commits landed in ONE day. Neither scope was in `_NOISE_SCOPES`, so
    the digest was about to present the owner with "Fixes implemented" that
    included a hypothesis I had since PROVED WRONG ("fix is likely dropping the
    keyword"), a retraction ("my fix may not be the fix"), and a claim I had
    corrected ("no per-call timeout" — there is one, 60 s). It also counted one
    incident five times.

    A note about a bug is not a bug fixed. The owner reads this list at 08:00 to
    learn what CHANGED; anything that only changed a document belongs elsewhere.
    """
    lines = [
        "b290e08d5 correction: the registry DOES thread sync actions — fix is likely dropping the keyword",
        "f3c886917 inbox: the 60s tool timeout cannot fire — every defi_data provider blocks the loop",
        "aaaaaaaaa note: the SAFETY batch hangs, not defi_data_portfolio specifically",
        "11504144e fix(defi): thread the blocking money-read verbs off the event loop",
        "c425b0028 perf(defi): one pooled HTTP client for the providers",
        "1c8b0c78a defi: thread the remaining 11 verbs, and ratchet async-in-name-only shut",
    ]
    fixes = ops_digest.parse_git_fixes(lines)
    assert fixes == [
        "thread the blocking money-read verbs off the event loop",
        "one pooled HTTP client for the providers",
        "thread the remaining 11 verbs, and ratchet async-in-name-only shut",
    ], fixes
    blob = " ".join(fixes).lower()
    for ghost in ("likely dropping the keyword", "cannot fire", "may not be the fix"):
        assert ghost not in blob, f"a filed observation reached the owner's fix list: {ghost!r}"


def test_a_noise_SUB_scope_is_noise_too():
    """`fix(inbox):` is the shape my own habit produces, and the prefix alone
    cannot catch it — `fix` is legitimate. Real example from 2026-09-23:
    `fix(inbox): the portfolio stall is an unbounded per-holding loop … —
    corrected from the code`, which is a note about a diagnosis, not a change to
    what runs. The parenthetical is the honest signal; read it.

    ⚠️ Only the NOTE sub-scopes count. My first version of this test also
    expected `feat(docs)` to be dropped, and the pre-existing
    `test_parse_git_log_filters_to_fix_feat` caught the over-reach: it pins
    `feat(ops): self-sufficient alerts` as a REAL feature, because a
    parenthetical names the AREA a change landed in, not its kind. The test was
    wrong, not the rule.
    """
    lines = [
        "b4b2c1484 fix(inbox): the portfolio stall is an unbounded per-holding loop — corrected from the code",
        "def456 feat(ops): self-sufficient alerts",
        "11504144e fix(defi): thread the blocking money-read verbs off the event loop",
    ]
    assert ops_digest.parse_git_fixes(lines) == [
        "self-sufficient alerts",
        "thread the blocking money-read verbs off the event loop",
    ]


def test_collect_asks_skips_a_fulfilled_or_closed_meta_block(tmp_path):
    """2026-09-22 08:09Z digest: two asks the owner had already settled — the
    gate-1 A/B pick (meta ends `— CLOSED**]`) and the OpenRouter top-up (meta
    starts `[FULFILLED BY OWNER …]`) — still rendered under "Asks (need you)"
    because neither carried one of the recognised resolution markers."""
    repo = _write_backlog(tmp_path, (
        "- [2026-09-20 10:20Z; ALERTED; **ANSWERED by the owner 10:16Z (= B); "
        "maint recorded B and fulfilled the ask — CLOSED**] **Gate-1 reference — A or B?** text\n"
        "- [FULFILLED BY OWNER 2026-09-20 ~16:30Z — balance $9.82] [was: 2026-09-20 01:15Z] "
        "**OpenRouter budget below the $3 line** text\n"
        "- [2026-09-19 12:45Z] **R6 ratchet-floor reading — \"A or B?\"** still open\n"
    ))
    assert ops_digest.collect_asks(repo) == ['R6 ratchet-floor reading — "A or B?"']
