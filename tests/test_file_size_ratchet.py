"""God-file size ratchet (F-2, 2026-07-17).

Freezes the known oversized modules at their current line counts and forbids
GROWTH. New behaviour belongs in a NEW file/mixin, not in one of these — that is
the repo's decomposition precedent (Agent/Orchestrator/MessageManager/Controller
mixin splits, UP-11). When a file is genuinely split, LOWER its ceiling here in
the same commit so the ratchet keeps tightening.

Mechanism (mirrors ``tests/test_path_ratchet.py``): per-file line-count ceilings
that may only SHRINK. ``wc -l`` semantics — we count ``"\n"`` so the numbers match
a shell ``wc -l``. Two guards:

- ``test_no_god_file_growth`` — a file exceeding its ceiling fails. This is the
  architectural pressure: don't grow the god-file, extract instead.
- ``test_ceilings_track_actual`` — a ceiling sitting far ABOVE the file's real
  size (a split that forgot to lower its row) fails, so the ratchet actually
  tightens. ``SLACK`` tolerates ordinary ±churn while still catching a real split
  (which removes hundreds of lines).
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# file -> max allowed line count (wc -l). SHRINK-ONLY: lower/delete rows when a
# file is split. Seeded 2026-07-17 (F-2 handoff) with the live sizes.
CEILINGS = {
    # Re-baselined 2026-07-19 (0.8.0 release cut): the 07-18/19 waves (019 et al.)
    # grew all six files past the 2026-07-17 seed without running this ratchet.
    # The extraction debt stands — these may still only SHRINK from here.
    "webview/server.py": 3824,  # F-2: api_agents/services/task/skills reads → agents/task/telemetry/
    "agents/task_agent_lite.py": 2593,
    "tools/controller/action_registration.py": 2571,
    "api/task_http_api.py": 1876,
    # Re-baselined 2026-07-25 (0.9.0 release cut): +3 net for the AUTONOMY_ENABLED
    # master state in `/autonomy` + `doctor`. The extraction debt on this file
    # STANDS — it is an extraction candidate, not a designated SSOT.
    "cli/ui/commands/handlers.py": 1908,
    # Re-baselined 2026-07-23: policy.py is the designated flag-accessor SSOT
    # (core/config_policy/) — this wave added run_budget_usd/_float_env,
    # compaction_prompt_guard, dead_target_registry_enabled, goal-blocked retry,
    # and related accessors, growing the file doing its designated job (see
    # AGENTS.md core/config_policy note). Not an extraction candidate.
    # Re-baselined again 2026-07-25 (0.9.0): the autonomy-default split added
    # _AUTONOMY_LOCAL_FLAGS + autonomy_enabled()/_autonomy_group_default() — the
    # same designated job.
    # Re-baselined 2026-08-08 (+12): defi_data_enabled() for the DeFi read tier.
    # It lives here rather than in tools/defi so that BOTH consumers (tool
    # registration and agents/task/tool_defaults) import DOWNWARD from one SSOT —
    # putting it in tools/ would have added a new agents->tools edge to the
    # layering ratchet's shrink-only allowlist and given the flag two readers
    # that could drift. Same designated job; not an extraction candidate.
    # Re-baselined 2026-08-14 (+13): the T4 money verbs (swap/approve_token/
    # revoke_approval) joined PAYMENT_APPROVAL_TOOLS — they had shipped on NO
    # approval lane (security review fix). Same designated job.
    "core/config_policy/policy.py": 1377,
}

# A ceiling may sit at most this many lines above the real size before the
# tightening guard demands it be lowered — generous enough for ordinary churn,
# tight enough that a real god-file split (hundreds of lines) forces the update.
SLACK = 150


def _line_count(rel: str) -> int:
    """wc -l of the repo-relative file (``"\n"`` count), or -1 if missing."""
    path = REPO / rel
    if not path.exists():
        return -1
    return path.read_text(errors="replace").count("\n")


def test_no_god_file_growth():
    over = {}
    for rel, ceiling in CEILINGS.items():
        n = _line_count(rel)
        if n > ceiling:
            over[rel] = (n, ceiling)
    assert not over, (
        "God-file(s) grew past their ceiling — extract new behaviour into a NEW "
        "module/mixin instead of growing these (see AGENTS.md decomposition note):\n"
        + "\n".join(f"  {f}: {n} > ceiling {c}" for f, (n, c) in sorted(over.items()))
    )


def test_ceilings_track_actual():
    """A split must lower its ceiling so the ratchet keeps tightening."""
    stale = {}
    for rel, ceiling in CEILINGS.items():
        n = _line_count(rel)
        if n < 0:
            continue  # a moved/renamed file — drop its row instead
        if ceiling - n > SLACK:
            stale[rel] = (ceiling, n)
    assert not stale, (
        "Ceiling(s) sit far above the real file size — lower them (shrink-only "
        "ratchet; a split shrinks its row):\n"
        + "\n".join(f"  {f}: ceiling {c} vs actual {n}" for f, (c, n) in sorted(stale.items()))
    )


def test_no_missing_ceiling_files():
    """Every ceilinged file must still exist (rename/delete → update the dict)."""
    missing = [rel for rel in CEILINGS if _line_count(rel) < 0]
    assert not missing, (
        "Ceilinged file(s) no longer exist — update CEILINGS:\n  " + "\n  ".join(sorted(missing))
    )
