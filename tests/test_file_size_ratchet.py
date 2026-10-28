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

W2 (2026-10-10 structural review): the ratchet used to cover only the files
someone remembered to list, so growth moved to un-listed files (the five largest
production files had no row). Now EVERY production ``.py`` file has a DEFAULT
ceiling (``DEFAULT_CEILING``); a file above it needs an explicit row in
``CEILINGS``, seeded at its live size. ``test_no_new_file_over_default`` is the
guard: a new or newly-grown file over the default fails — split it instead.
"""
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# file -> max allowed line count (wc -l). SHRINK-ONLY: lower/delete rows when a
# file is split. Seeded 2026-07-17 (F-2 handoff) with the live sizes.
CEILINGS = {
    # Re-baselined 2026-07-19 (0.8.0 release cut): the 07-18/19 waves (019 et al.)
    # grew all six files past the 2026-07-17 seed without running this ratchet.
    # The extraction debt stands — these may still only SHRINK from here.
    # Tightened 2026-09-15 (043 W13): the account + admin page surfaces
    # (/signin, /profile, /settings, /admin*) moved to webview/posture_routes.py
    # as ONE posture table — they were seven hand-registered routes that
    # disagreed about what a denial looks like.
    "webview/server.py": 2992,  # 043 A12/A29/A30: the full-tree session catalog, the per-session stats route and the four consumer-less socket emits deleted; 070 W0.10: the feed events route moved to webview/feed_routes.py; 070 W0.14: the workspace tree moved to webview/workspace_routes.py; 070 W0.17: socket joins through webview/socket_limits.py; 070 W0.18/E.34: the error handler delegates to webview/error_page.py
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
    "tools/controller/action_registration.py": 1833,
    "api/task_http_api.py": 1876,
    # Re-baselined 2026-07-25 (0.9.0 release cut): +3 net for the AUTONOMY_ENABLED
    # master state in `/autonomy` + `doctor`. The extraction debt on this file
    # STANDS — it is an extraction candidate, not a designated SSOT.
    "cli/ui/commands/handlers.py": 1790,
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
    # Raised 2026-09-15 with the two models it names (+50): z-ai/glm-5.3 and
    # z-ai/glm-5.3-flash, the ids the GLM Coding Plan seat actually serves.
    # Raised 2026-09-23 with the two models it names (+36): claude-opus-5 and
    # claude-fable-5-1 — the first Anthropic rows that accept mid-conversation
    # tool changes (F9 shape (b)), plus the comment that says which do not.
    "modules/llm/model_catalog.py": 1553,
    # S6 (2026-08-29): scan / notify / subscriptions / reputation extracted to settlement_*.py mixins.
    "modules/x402/settlement_watcher.py": 330,
    "modules/llm/gemini_client.py": 1360,
    # Raised 2026-09-15 (+16): the glm-5.3 / glm-5.3-flash ids on BOTH z.ai rows,
    # plus the notes recording why zai-coding's provider-wide supports_vision stays
    # False while glm-5.3-flash has vision, and why the metered `zai` row keeps its
    # cheaper default. No new behaviour — two existing data rows.
    "modules/llm/provider_spec.py": 1317,
    # S5 (2026-08-29): curated-notes / KB / episodes stores extracted to sqlite_*_store.py mixins.
    # 2026-09-22 (WS-K4): 492 -> 491. The recall-consolidation BEHAVIOUR went to a
    # new module (modules/memory/recall_consolidation.py); only the mixin wiring
    # landed here, paid for by trimming a dated change-story out of a docstring.
    # 2026-09-23 (025): 491 -> 466. prune_memories moved to the new scope/retention
    # mixin (modules/memory/sqlite_scope_store.py) with the scope verbs.
    "modules/memory/sqlite_memory_provider.py": 320,
    "modules/x402/invoicing.py": 1169,
    "modules/llm/adapters.py": 1111,
    "modules/memory/task/task_context_manager.py": 1135,
    "modules/llm/openrouter_client.py": 960,
    # Tightened 2026-09-08: the read-only client/model inventory
    # (get_available_models / get_available_clients) moved to
    # modules/llm/manager_inventory.py::InventoryMixin, which the manager composes.
    "modules/llm/llm_manager.py": 861,
    "modules/llm/llm_client.py": 955,
    "modules/llm/anthropic_client.py": 920,  # F12: breakpoint placement -> cache_hints.py; sampling routing + thinking clamp -> anthropic_sampling.py
    "modules/llm/openai_client.py": 901,
    "modules/memory/task/hierarchical_memory.py": 821,
    # W2 (2026-10-10): every production file over DEFAULT_CEILING, frozen at its
    # live size. Each is a split candidate (structural review dim 3, W9); lower
    # the row in the commit that splits the file, delete it once <= the default.
    "surfaces/telegram/harness.py": 2934,  # W9: admin verbs -> admin_verbs.py + *_ops.py
    "core/wallet/tx_guard.py": 1579,  # W6: _authorize -> core/wallet/tx_guard_steps/
    "packs/x/polyrob_x/twitter_tool.py": 2824,
    "packs/markets/polyrob_markets/polymarket/service.py": 2184,
    "packs/markets/polyrob_markets/hyperliquid/service.py": 1907,
    "webview/pages_new.py": 1858,
    "cli/commands/wallet.py": 1841,
    "webview/pages.py": 1841,
    "tools/browser/context.py": 1793,
    "cli/commands/owner.py": 1646,
    "agents/task/agent/core/next_action_internal.py": 1616,
    "tools/browser/browser.py": 1604,
    "core/prefs.py": 1569,
    "tools/controller/registry/service.py": 1552,
    "api/admin_endpoints.py": 1328,
}

# Every production .py file not listed above may hold at most this many lines.
DEFAULT_CEILING = 1500

# The production tree: the shipped packages (+ the entry point). Tests, scripts,
# datagen and docs are not production; generated files are excluded explicitly.
PROD_ROOTS = (
    "agents/", "api/", "cli/", "core/", "cron/", "migrations/", "modules/",
    "packs/", "surfaces/", "tools/", "utils/", "webview/", "main.py",
)
GENERATED = {
    "core/flags_catalog.py",  # generated from docs/CONFIGURATION.md
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


def _is_production(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    if not rel.endswith(".py") or rel in GENERATED:
        return False
    if any(p in ("tests", "test") for p in parts[:-1]):
        return False
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return False
    return rel.startswith(PROD_ROOTS)


def _production_files():
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"], cwd=REPO, capture_output=True, text=True,
    ).stdout
    return sorted(f for f in out.split("\0") if f and _is_production(f))


def test_no_new_file_over_default():
    """Any production file without a row may not exceed DEFAULT_CEILING."""
    over = {}
    for rel in _production_files():
        if rel in CEILINGS:
            continue
        n = _line_count(rel)
        if n > DEFAULT_CEILING:
            over[rel] = n
    assert not over, (
        f"Production file(s) over the default ceiling ({DEFAULT_CEILING} lines) — split "
        "the file (a new module/mixin), do not add a CEILINGS row for new growth:\n"
        + "\n".join(f"  {f}: {n}" for f, n in sorted(over.items()))
    )


def test_no_god_file_growth():
    over = {}
    for rel, ceiling in CEILINGS.items():
        n = _line_count(rel)
        if n > ceiling:
            over[rel] = (n, ceiling)
    assert not over, (
        "God-file(s) grew past their ceiling — extract new behaviour into a NEW "
        "module/mixin instead of growing these:\n"
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
