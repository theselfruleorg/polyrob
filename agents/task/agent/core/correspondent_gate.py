"""WS-A capability gate — block high-impact tools while a session is "tainted" by
untrusted correspondent DATA (Fusion HIGH/Q9).

Correspondent replies enter as a user-role control message with prompt-level framing
(`<correspondent-message>` + `<untrusted_tool_result>`). That framing is a SOFT defense:
a strong injection could still try to drive a tool. This pre-tool-call hook is the
STRUCTURAL backstop — while the latest input on a session is correspondent-untrusted,
the dangerous tools (money, outbound messaging, code execution, delegation, browser)
are denied, so a forged email can never directly spend/send/execute. The owner clears
the taint simply by sending a genuine message (they are driving again).

Pure policy + a hook factory; the taint flag lives on the orchestrator (set on
correspondent injection, cleared on owner intake).

⚠️ **tool_id vs action-name (the load-bearing detail).** The pre-tool-call hook receives
the bare *action name* — ``run_code``, ``goal_create``, ``x402_fetch`` — never the
*tool_id* (``code_execution``, ``goal``, ``x402_pay``). A denylist keyed on tool_ids is
therefore dead: it only ever fires for a tool whose single action is literally named
after the tool_id. So this module blocks in two layers:

  1. ``_HIGH_IMPACT_NAMES`` — explicit high-impact *action* names (delegation, skill/
     self-context, crypto trade verbs, git/github write verbs). Matched by name.
  2. ``HIGH_IMPACT_TOOL_IDS`` — whole tools whose every non-read action is high-impact,
     matched by RESOLVING the action's owning tool_id at hook time (via a resolver the
     wiring passes in, backed by ``Controller.get_action_details(name).tool``). This is
     the only way to cover dynamically-named actions such as MCP direct actions
     (``{server}_{tool}`` → tool_id ``mcp``).

Crypto tools (``hyperliquid``/``polymarket``) are deliberately EXCLUDED from
``HIGH_IMPACT_TOOL_IDS`` so their read verbs (``get_*``) stay allowed — a tainted
session may still answer "what's the price?"; their trade verbs are enumerated by name.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Layer 1 — high-impact *action* names (matched against the bare action name the
# pre-hook receives). Includes standalone actions (registered directly, with no
# owning container tool_id) plus enumerated verbs kept for defense-in-depth even
# when tool-id resolution is unavailable. Crypto READ verbs are deliberately absent.
# ---------------------------------------------------------------------------
_HIGH_IMPACT_NAMES = frozenset({
    # Delegation actions (registered directly, not container tools).
    "delegate_task", "subtask", "parallel_subtasks",
    # Identity / self-evolution actions (registered directly).
    "skill_manage", "self_context_manage",
    # Gated outbound message-to-target action (registered directly, no owning
    # tool_id) — owner/allowlist-checked send to telegram/email/whatsapp.
    "message",
    # Aspirational coding/self-evolution action names (no tool yet; harmless tokens).
    "self_modify", "mcp_install",
    # WS-5: self_env self-maintenance verbs (posture 2). Owner-only via the posture
    # gate already, but a tainted session must never reach them either.
    "self_env_install_dep", "self_env_patch_source", "self_env_restart_service",
    "self_env_git_pull", "self_env_read_source", "shell_run",
    # WS-2/3: process job-manager verbs — enumerated by NAME (parity with shell_run)
    # so a tool-id-resolver fault can't let a tainted session kill/inspect the owner's
    # background shell jobs.
    "process_kill", "process_log", "process_poll", "process_list",
    # P1-4: code-exec verb enumerated by NAME (parity with shell_run) so a resolver
    # fault can't open arbitrary code execution to a tainted session (the code_execution
    # tool_id below only helps when resolution succeeds).
    "run_code",
    # P1-4: the agent money verb — x402_invoice tool, verb x402_request — mints a
    # payment request. The canonical forged-email social-engineering target; must be
    # unreachable while correspondent-tainted (x402_fetch/x402_pay already are).
    "x402_request",
    # P1-4: outbound-egress verbs whose query params are an exfil channel (parity with
    # web_fetch/browser, which are already blocked). anysite/perplexity reach the
    # outside world with attacker-influenced arguments.
    "anysite_api", "perplexity_search",
    # P1-4: curated-memory write persists into FUTURE sessions' prompts — a durable
    # injection-persistence channel if written while tainted. Gate the whole action
    # (read too) — fail-closed; the owner clears taint by replying.
    "memory",
    # Crypto trade verbs (hyperliquid + polymarket share these). Reads (get_*) are
    # intentionally excluded so a tainted session can still fetch prices/history.
    "place_limit_order", "place_market_order", "cancel_order", "cancel_all_orders",
    "update_leverage", "approve_agent", "revoke_agent",
    # git/github write verbs — also covered by tool-id resolution, kept here so a
    # resolver fault can't open the money/ship-code path.
    "git_push", "github_open_pr", "github_merge_pr", "github_pr_comment",
    "github_issue_create",
    # The auto-paying x402 action (the tool_id is x402_pay; the verb is x402_fetch).
    "x402_fetch",
    # Legacy tool_id tokens kept so is_high_impact(tool_id) stays truthy for callers/
    # tests that probe by tool_id. Real per-verb coverage of these tools comes from
    # HIGH_IMPACT_TOOL_IDS resolution below.
    "code_execution", "coding", "cronjob", "goal", "x402_pay",
    "hyperliquid", "polymarket", "email", "twitter", "browser", "web_fetch",
    "git", "github", "process", "tool_manage", "mcp",
    "x402_invoice", "anysite", "perplexity",  # P1-4 legacy tool_id tokens
})

# Back-compat public name (tests / other callers import HIGH_IMPACT_TOOLS).
HIGH_IMPACT_TOOLS = _HIGH_IMPACT_NAMES

# ---------------------------------------------------------------------------
# Layer 2 — tool_ids whose EVERY action is high-impact, resolved at hook time from
# the action name. Crypto is intentionally absent (its reads must stay allowed; its
# trade verbs are in _HIGH_IMPACT_NAMES). Blocking a high-impact tool's read verbs
# too (e.g. goal_list, cronjob_list, git_log) while tainted is the safe/fail-closed
# direction — the owner clears the taint by replying.
# ---------------------------------------------------------------------------
HIGH_IMPACT_TOOL_IDS = frozenset({
    "code_execution",  # run_code — arbitrary code exec
    "coding",          # str_replace / apply_patch / run_tests / *_file — repo mutation
    "cronjob",         # cronjob_schedule / cancel — durable recurring runs
    "goal",            # goal_create / objective_* — durable autonomous work
    "x402_pay",        # x402_fetch — auto-pay
    "email",           # send_email — outbound comms
    "twitter",         # twitter_post / reply / quote / thread — outbound comms
    "browser",         # go_to_url / click_element / … — SSRF / exfil
    "web_fetch",       # fetch_url — outbound fetch (SSRF / exfil)
    "git",             # git_commit / clone / pull / push — ship code / identity
    "github",          # github_open_pr / merge / … — ship code / identity
    "mcp",             # execute_tool + dynamic {server}_{tool} — outbound / exec
    "process",         # background processes (aspirational)
    "tool_manage",     # dynamic-tool authoring (aspirational)
    "shell",           # WS-2: persistent sandbox shell — arbitrary command exec
    "self_env",        # WS-5: self-maintenance verbs — install/patch/restart/pull
    # P1-4:
    "x402_invoice",    # x402_request / accounting — mint agent payment requests (money)
    "anysite",         # anysite_api — outbound structured-data egress (exfil channel)
    "perplexity",      # perplexity_search — outbound search egress (exfil channel)
})

# Substrings that mark a high-impact action even if the exact name isn't enumerated
# (e.g. provider-prefixed MCP/web tools that reach the outside world). NOTE: do NOT add
# "trade" here — as a substring it falsely flags the read action get_trade_history while
# matching no real trade verb (those are enumerated above).
_HIGH_IMPACT_PREFIXES = ("mcp_", "browser_", "web_", "email_", "pay", "send_email")


def is_high_impact(action_name: Optional[str]) -> bool:
    """Name-only high-impact test (no tool_id resolution).

    True if ``action_name`` is an enumerated high-impact action name (or legacy
    tool_id token) or matches a high-impact substring. This is the backward-compatible
    surface; the wired hook additionally resolves the owning tool_id via
    :func:`is_high_impact_call`.
    """
    if not action_name:
        return False
    name = str(action_name).strip().lower()
    if name in _HIGH_IMPACT_NAMES:
        return True
    return any(name.startswith(p) or p in name for p in _HIGH_IMPACT_PREFIXES)


def is_high_impact_call(action_name: Optional[str], tool_id: Optional[str] = None) -> bool:
    """Full high-impact decision for one action call.

    Blocks when the bare action name is high-impact (:func:`is_high_impact`) OR when the
    action's owning ``tool_id`` is in :data:`HIGH_IMPACT_TOOL_IDS`. ``tool_id`` is the
    resolved owning tool (``Controller.get_action_details(name).tool``); pass ``None``
    when it can't be resolved, in which case the decision degrades to the name-only path.
    """
    if is_high_impact(action_name):
        return True
    if tool_id and str(tool_id).strip().lower() in HIGH_IMPACT_TOOL_IDS:
        return True
    return False


def build_tool_resolver(controller: Any) -> Callable[[str], Optional[str]]:
    """Return ``action_name -> owning tool_id`` backed by a Controller.

    Uses ``Controller.get_action_details(name).tool`` — the same registry seam the
    untrusted-wrap uses to resolve an action's source tool. Never raises: a missing
    controller, an unknown action, or a registry fault all degrade to ``None`` (the
    caller then falls back to the name-only decision).
    """
    def _resolve(action_name: str) -> Optional[str]:
        if controller is None:
            return None
        try:
            details = controller.get_action_details(action_name)
            return getattr(details, "tool", None) if details is not None else None
        except Exception as e:
            logger.debug("build_tool_resolver: get_action_details failed for %r: %s",
                         action_name, e)
            return None
    return _resolve


def make_correspondent_gate_hook(
    get_tainted: Callable[[], bool],
    resolve_tool: Optional[Callable[[str], Optional[str]]] = None,
):
    """Pre-tool-call hook ``(action_name, params, context) -> Optional[str]``.

    Returns a denial reason (string) for a high-impact tool while the session is tainted
    by untrusted correspondent data; None otherwise. Fail-CLOSED: if the taint probe
    raises, a high-impact tool is denied (we can't prove the session is clean).

    ``resolve_tool`` maps an action name to its owning tool_id so a tool_id-level
    denylist (:data:`HIGH_IMPACT_TOOL_IDS`) actually fires — the pre-hook only ever sees
    the bare action name. It is called defensively: a resolver fault degrades to the
    name-only decision (never raises out of the hook, never silently opens a hole for a
    name-level high-impact action).
    """
    def _hook(action_name: str, params: Any, context: Any) -> Optional[str]:
        tool_id: Optional[str] = None
        if resolve_tool is not None:
            try:
                tool_id = resolve_tool(action_name)
            except Exception as e:  # resolution must never break the gate
                logger.debug("correspondent gate tool resolve failed: %s", e)
                tool_id = None
        if not is_high_impact_call(action_name, tool_id):
            return None
        try:
            tainted = bool(get_tainted())
        except Exception as e:  # fail-closed: can't prove clean -> deny the dangerous tool
            logger.debug("correspondent gate taint probe failed (deny): %s", e)
            tainted = True
        if tainted:
            return (f"'{action_name}' is blocked: the latest input is untrusted "
                    f"correspondent DATA — owner confirmation is required before a "
                    f"high-impact action.")
        return None
    return _hook
