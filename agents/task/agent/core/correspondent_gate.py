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
    # owner-UX P2 T2: agent-callable config/contract action (registered
    # directly, no owning tool_id) — a correspondent-tainted session must not
    # read OR change tenant preferences / propose operating-contract rules.
    "preferences",
    # Gated outbound message-to-target action (registered directly, no owning
    # tool_id) — owner/allowlist-checked send to telegram/email/whatsapp.
    "message",
    # I-6: read-only runtime introspection (registered directly, no owning
    # tool_id) — reveals wallet balance + tenant ledger, the same money data the
    # gate deliberately blocks via x402_pay/x402_invoice tool-id membership.
    # Info-disclosure only, but a correspondent-tainted turn must not read it.
    "agent_status",
    # Task 13 (Phase 3 R3): read-only tenant usage rollup + suggested-invoice
    # draft (registered directly, no owning tool_id). Same info-disclosure
    # reasoning as agent_status — a tainted session must not read cost data
    # or see an invoice-draft suggestion (a social-engineering target: a
    # forged correspondent could otherwise fish for "how much would you
    # invoice me").
    "usage_summary",
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
    # hf_deploy verbs enumerated by NAME (parity with run_code/shell_run) so a
    # resolver fault can't let a tainted session publish/delete a PUBLIC HF Space
    # (the hf_deploy tool_id below only helps when resolution succeeds).
    "deploy", "undeploy",
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
    "hf_deploy",  # legacy tool_id token (deploy/undeploy — see HIGH_IMPACT_TOOL_IDS)
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
# WS-2 (2026-07-16): derived from the ONE per-tool capability table
# (core/tool_capabilities.py, `high_impact`) — classify a new tool there, not here;
# per-entry rationale lives with the table rows. Parity-pinned by
# tests/unit/core/test_tool_capabilities.py. The trading venues' deliberate absence is
# now explicit in the table (`readable_while_tainted`).
from core.tool_capabilities import ids_with as _ids_with

HIGH_IMPACT_TOOL_IDS = _ids_with("high_impact")

# Substrings that mark a high-impact action even if the exact name isn't enumerated
# (e.g. provider-prefixed MCP/web tools that reach the outside world). NOTE: do NOT add
# "trade" here — as a substring it falsely flags the read action get_trade_history while
# matching no real trade verb (those are enumerated above).
_HIGH_IMPACT_PREFIXES = ("mcp_", "browser_", "web_", "email_", "pay", "send_email")

# H10 (2026-07-15): the crypto TRADE verbs, matched as substrings so BOTH the bare
# name AND every venue-namespaced runtime form are caught. Container-tool actions
# register namespaced (polymarket_place_limit_order / hyperliquid_place_market_order),
# and that namespaced name is what reaches the gate hook — but crypto tool_ids are
# deliberately absent from HIGH_IMPACT_TOOL_IDS (reads must stay allowed), so bare-name
# matching alone let the namespaced trade verb slip the gate. Unlike "trade", none of
# these substrings appears in a read verb (reads are get_*). Keep in sync with the trade
# verbs enumerated in _HIGH_IMPACT_NAMES.
_HIGH_IMPACT_VERB_SUBSTRINGS = (
    "place_limit_order", "place_market_order", "cancel_order",
    "cancel_all_orders", "update_leverage", "approve_agent", "revoke_agent",
)


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
    if any(sub in name for sub in _HIGH_IMPACT_VERB_SUBSTRINGS):
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


# ---------------------------------------------------------------------------
# D1 (2026-07-13 review): scoped reply-while-tainted. Every correspondent reply
# re-taints the session and taint blocks ALL outbound comms, so the agent could
# never answer the person who just wrote in — every round needed an owner turn.
# The exemption below permits message/send_email to EXACTLY the tainting
# (surface, address): 1:1, no cc/bcc, budget- and flag-gated.
# ---------------------------------------------------------------------------
_REPLY_ACTIONS = frozenset({"message", "send_email"})


def _param(params: Any, key: str) -> Any:
    if params is None:
        return None
    if isinstance(params, dict):
        return params.get(key)
    return getattr(params, key, None)


def _reply_target(action_name: str, params: Any) -> tuple:
    """(surface, address) this call would message, or ('', '') when not a clean
    single-recipient reply shape (multi-recipient / cc / bcc are never exempt)."""
    name = str(action_name or "").strip().lower()
    if name == "message":
        return (str(_param(params, "surface") or ""),
                str(_param(params, "target") or ""))
    if name == "send_email":
        if _param(params, "cc") or _param(params, "bcc"):
            return ("", "")
        to = _param(params, "to_email")
        if isinstance(to, (list, tuple)):
            to = to[0] if len(to) == 1 else None
        return ("email", str(to or ""))
    return ("", "")


def _is_scoped_reply(action_name: str, params: Any, sources: Any) -> tuple:
    """(is_reply_to_tainting_party, surface, address)."""
    surface, address = _reply_target(action_name, params)
    if not surface or not address:
        return (False, surface, address)
    key = (surface, address.strip().lower())
    return (key in (sources or set()), surface, address.strip().lower())


def build_reply_allowed(
    get_container: Callable[[], Any],
    get_user_id: Callable[[], str],
) -> Callable[[str, str], bool]:
    """Policy for the scoped tainted-reply exemption: flag + rounds budget.

    Denies unless ``CORRESPONDENT_REPLY_ENABLED`` (default OFF) AND the tenant has
    sent fewer than ``CORRESPONDENT_REPLY_MAX_ROUNDS`` outbound messages to that
    address in the last 24h (counted from the ConversationStore). Fail-CLOSED —
    if the budget can't be verified, the reply stays blocked (the owner can
    always unblock by replying).
    """
    def _allowed(surface: str, address: str) -> bool:
        try:
            from agents.task.surface_config import SurfaceConfig
            if not SurfaceConfig.correspondent_reply_enabled():
                return False
            max_rounds = SurfaceConfig.correspondent_reply_max_rounds()
            container = get_container()
            store = (container.get_service("conversation_store")
                     if container else None)
            if store is None:
                # Flag explicitly ON but no budget substrate — allow (the flag is
                # the operator's informed opt-in; without a store there is no
                # rounds history to enforce).
                return True
            user_id = get_user_id() or ""
            return store.outbound_count_since(user_id, surface, address,
                                              86400) < max_rounds
        except Exception as e:  # fail-closed
            logger.debug("reply_allowed probe failed (deny): %s", e)
            return False
    return _allowed


def make_correspondent_gate_hook(
    get_tainted: Callable[[], bool],
    resolve_tool: Optional[Callable[[str], Optional[str]]] = None,
    get_taint_sources: Optional[Callable[[], Any]] = None,
    reply_allowed: Optional[Callable[[str, str], bool]] = None,
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

    ``get_taint_sources`` + ``reply_allowed`` enable the D1 scoped-reply exemption:
    while tainted, ``message``/``send_email`` to EXACTLY a tainting (surface, address)
    is permitted when ``reply_allowed(surface, address)`` approves (flag + rounds
    budget). Everything else stays denied.
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
            # D1 scoped-reply exemption (never widens beyond the tainting party).
            if (get_taint_sources is not None and reply_allowed is not None
                    and str(action_name or "").strip().lower() in _REPLY_ACTIONS):
                try:
                    is_reply, surface, address = _is_scoped_reply(
                        action_name, params, get_taint_sources())
                    if is_reply and reply_allowed(surface, address):
                        logger.info(
                            "correspondent gate: scoped tainted reply to %s:%s "
                            "permitted", surface, address)
                        return None
                except Exception as e:  # fail-closed: exemption probe never opens
                    logger.debug("scoped-reply exemption probe failed (deny): %s", e)
            return (f"'{action_name}' is blocked: the latest input is untrusted "
                    f"correspondent DATA — owner confirmation is required before a "
                    f"high-impact action.")
        return None
    return _hook
