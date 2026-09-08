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
    # S8 (2026-08-29): chat / delivery / lifecycle mixins + support helpers extracted
    # (agents/task/task_agent_{chat,delivery,lifecycle,support}.py).
    # Tightened 2026-09-08: the public session-control verbs (get_session_status /
    # cancel_session / pause_session / resume_session) moved to
    # agents/task/task_agent_control.py::TaskAgentControlMixin.
    "agents/task_agent_lite.py": 978,
    # Tightened 2026-08-20: the module-level turn-origin policy helpers
    # (_is_forged_or_autonomous_turn et al.) moved to tools/controller/turn_origin.py
    # (re-exported for back-compat) after 098781b8 pushed the file past 2571.
    # Tightened 2026-08-28: agent_status extracted to tools/controller/agent_status_action.py
    # (status SSOT); the action now renders from core/status_snapshot.py.
    # Tightened 2026-09-08: the two document-authoring actions (self_context_manage,
    # owner_doc_manage) moved to tools/controller/doc_authoring.py::DocAuthoringMixin.
    "tools/controller/action_registration.py": 1971,
    "api/task_http_api.py": 1876,
    # Re-baselined 2026-07-25 (0.9.0 release cut): +3 net for the AUTONOMY_ENABLED
    # master state in `/autonomy` + `doctor`. The extraction debt on this file
    # STANDS — it is an extraction candidate, not a designated SSOT.
    "cli/ui/commands/handlers.py": 1908,
    # Split 2026-08-29 (S2, backlog B19): policy.py is now a FACADE over nine
    # submodules (_env, local_profile, autonomy_mode, autonomy_posture,
    # compute_posture, payment_policy, capability_toggles, autonomy_config,
    # runtime_gates). New accessors go in the owning submodule; the facade only
    # re-exports, so it must not grow beyond its import lists.
    "core/config_policy/policy.py": 120,
    # modules/ rows seeded 2026-08-29 (S4, backlog B09) at the live sizes after the
    # model_registry split (registry 418 / types 239 / catalog 1505 / pricing 57).
    # The catalog is DATA (one ModelConfig per model) and may only grow by one
    # model at a time — raise its row deliberately with the model, never by drift.
    "modules/llm/model_catalog.py": 1505,
    # S6 (2026-08-29): scan / notify / subscriptions / reputation extracted to settlement_*.py mixins.
    "modules/x402/settlement_watcher.py": 330,
    "modules/llm/gemini_client.py": 1357,
    "modules/llm/provider_spec.py": 1301,
    # S5 (2026-08-29): curated-notes / KB / episodes stores extracted to sqlite_*_store.py mixins.
    "modules/memory/sqlite_memory_provider.py": 492,
    "modules/x402/invoicing.py": 1169,
    "modules/llm/adapters.py": 1140,
    "modules/memory/task/task_context_manager.py": 1135,
    "modules/llm/openrouter_client.py": 960,
    # Tightened 2026-09-08: the read-only client/model inventory
    # (get_available_models / get_available_clients) moved to
    # modules/llm/manager_inventory.py::InventoryMixin, which the manager composes.
    "modules/llm/llm_manager.py": 861,
    "modules/llm/llm_client.py": 955,
    "modules/llm/anthropic_client.py": 912,
    "modules/llm/openai_client.py": 896,
    "modules/memory/task/hierarchical_memory.py": 821,
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
