"""H1 (2026-07-14 review): the migration guide must not contradict comparison.md.

from-hermes.md marked Discord/Slack/Signal ❌ for POLYROB months after they shipped —
telling prospects to stay on Hermes for things POLYROB does. This guards the platform
claims in both flagship docs against re-diverging.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SHIPPED_SURFACES = ("Discord", "Slack", "Signal", "X (DM)")


def _platform_row(text: str, platform: str) -> list:
    """Cells of the `| **<platform>** | ... |` table row, or []."""
    pattern = re.compile(rf"^\|\s*\*\*{re.escape(platform)}\*\*\s*\|(.*)$", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return []
    return [c.strip() for c in m.group(1).split("|") if c.strip()]


def test_from_hermes_platform_table_marks_shipped_surfaces_supported():
    text = (REPO_ROOT / "docs/guide/migration/from-hermes.md").read_text()
    for platform in SHIPPED_SURFACES:
        cells = _platform_row(text, platform)
        assert cells, f"platform row for {platform!r} missing from from-hermes.md"
        polyrob_cell = cells[-1]  # columns: Hermes | POLYROB
        assert not polyrob_cell.startswith("❌"), (
            f"from-hermes.md marks {platform} ❌ for POLYROB — it shipped 2026-07-12; "
            f"keep the guide consistent with docs/comparison.md")


def test_comparison_md_lists_shipped_surfaces():
    text = (REPO_ROOT / "docs/comparison.md").read_text()
    m = re.search(r"^\|\s*\*\*Multi-Surface\*\*\s*\|([^|]*)\|", text, re.MULTILINE)
    assert m, "Multi-Surface row missing from comparison.md"
    cell = m.group(1)
    for name in ("Discord", "Slack", "Signal", "X"):
        assert name in cell, f"comparison.md Multi-Surface row lost {name!r}"


# ---------------------------------------------------------------------------
# H4 (2026-07-14 review): plan/review docs referenced from committed docs must
# themselves be committed. docs/* is gitignored, so plan docs need `git add -f`;
# the Hermes-parity design chain was never committed and 21 referenced docs are
# now UNRECOVERABLE (see docs/reviews/2026-07-14-hermes-parity-SSOT.md §4).
# ---------------------------------------------------------------------------

# Docs already lost before the rule existed. NEVER add to this list — commit
# your plan docs instead (`git add -f docs/plans/<file>`).
_LOST_BEFORE_ENFORCEMENT = {
    # WS revalidation 2026-07-16: these three were referenced by docs that were
    # THEMSELVES untracked until the same-day recovery sweep (8 docs restored from
    # history via `git show <adding-sha>:<path>`); the three below were never
    # committed anywhere in history (checked `git log --all --diff-filter=A`) —
    # genuine pre-enforcement losses, surfaced only because their referrers are
    # now tracked.
    "docs/reviews/2026-07-03-session-memory-vs-hermes.md",
    "docs/reviews/2026-07-06-computer-use-capability-vs-hermes.md",
    "docs/reviews/2026-07-11-owner-ux-monitoring-gap-analysis.md",
    "docs/plans/2026-06-17-terminal-native-consolidation.md",
    "docs/plans/2026-06-21-polyrob-analyze-and-implement-HANDOFF.md",
    "docs/plans/2026-06-30-rob-launch-and-operations-design.md",
    "docs/plans/2026-07-01-polyrob-oss-release-0.4.2-DESIGN.md",
    "docs/plans/2026-07-03-owner-instance-identity-model-HANDOFF.md",
    "docs/plans/2026-07-03-permissions-system-audit-FINDINGS.md",
    "docs/plans/2026-07-03-permissions-system-audit-HANDOFF.md",
    "docs/plans/2026-07-03-polyrob-avatars-FINALIZED-upgrade-instructions.md",
    "docs/plans/2026-07-03-rob-runs-and-capabilities-review-HANDOFF.md",
    "docs/plans/2026-07-03-rob-runs-and-capabilities-review-REPORT.md",
    "docs/plans/2026-07-04-autonomy-learning-evolution-vs-hermes-REVIEW.md",
    "docs/plans/2026-07-04-intelligence-wiring-groupE-recommendations.md",
    "docs/plans/2026-07-10-harness-coding-unified-remediation.md",
    "docs/plans/2026-07-10-harness-issues-remediation.md",
    "docs/plans/2026-07-11-hermes-infra-parity-design.md",
    "docs/plans/autonomy-loops-FINALIZED-2026-06-16.md",
    "docs/reviews/2026-07-06-structural-review.md",
    "docs/reviews/2026-07-09-critical-runtime-review.md",
    "docs/reviews/2026-07-11-hermes-datagen-connectors-review.md",
    "docs/reviews/2026-07-11-memory-knowledge-review-and-wiki-proposal.md",
}


def test_referenced_plan_docs_are_committed():
    import subprocess
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True).stdout.split()
    tracked_set = set(tracked)
    ref_re = re.compile(r"docs/(?:plans|reviews)/[A-Za-z0-9._-]+\.md")
    hits = []
    for rel in tracked:
        if not rel.endswith(".md"):
            continue
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        for ref in set(ref_re.findall(path.read_text())):
            if ref in tracked_set or ref in _LOST_BEFORE_ENFORCEMENT:
                continue
            hits.append(f"{rel} references uncommitted {ref}")
    assert not hits, (
        "Plan/review docs referenced from committed docs must be committed "
        "(git add -f — docs/* is gitignored). Do NOT grandfather new losses:\n"
        + "\n".join(sorted(hits)))
