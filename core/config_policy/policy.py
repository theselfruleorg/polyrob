"""Cross-cutting config/policy surface for POLYROB — the autonomy/mode/posture/payment
policy cluster and AutonomyConfig, relocated from agents/task/constants.py (WS-1, 2026-07-16)
so it lives in the core tier and the core<->agents.task import cycle is broken.

This module imports ONLY stdlib + core.env (+ lazy core.instance /
core.security.forged_turns), so `import core.config_policy` never reaches agents.task. agents/task/constants.py re-exports
every public and externally-referenced private name here, so existing importers are
unaffected. New code should import from core.config_policy. See
docs/plans/2026-07-16-ws1-config-relocation.md.
"""

import logging
import os

from core.env import bool_env as _core_bool_env, int_env as _core_int_env


# --- Autonomy & continuous-learning loops (Reference-parity, 2026-06-16) ---------
#
# Shared flag helpers for the four loops POLYROB lacked: self-wake re-entry,
# writable-skills + background-review, cron run-loop+delivery, durable goal board,
# and the curator. SINGLE SOURCE OF TRUTH for the falsey-set semantics, mirroring
# reflection_llm_enabled_default() above. All loops default-OFF + fail-open except
# MEMORY_SEARCH_TOOL (read-only, tenant-scoped) and CRON_RUN_LOOP (fixes a live bug
# where cron built a session but never ran the agent loop).

_FALSEY = ("none", "off", "false", "0", "no", "")


def _bool_env(name: str, default: bool) -> bool:
    """Read a boolean env var with POLYROB's falsey-set semantics.

    Delegates to the repo-wide SSOT (``core.env.bool_env``) so this module shares
    one parser with everything else instead of reimplementing it (the reflection-gate
    bug was a parser/source mismatch). Kept as a thin wrapper so in-module callers
    (and this module's public ``_bool_env`` symbol) are unaffected.
    """
    return _core_bool_env(name, default)


def _int_env(name: str, default: int) -> int:
    """Delegates to the ONE int parser (core.env.int_env); name kept for re-export."""
    return _core_int_env(name, default)


# --- Local (terminal-native, single-user) profile -------------------------
# When POLYROB_LOCAL is truthy, the *safe* autonomy/learning flags default ON as a
# group, so a terminal user gets the W1-W7 loops without setting ~6 env vars.
# Multi-tenant server entry (main.py / uvicorn) never sets POLYROB_LOCAL, so its
# defaults are unchanged. An explicit per-flag value (e.g. GOALS_ENABLED=off)
# still wins — only the *default* moves.
#
# Excludes anything with a multi-tenant blast radius even on one machine:
# CODE_EXEC_ENABLED (not a sandbox) and the sub-agent concurrency caps.
_SAFE_LOCAL_FLAGS = frozenset({
    "SELF_WAKE_ENABLED",
    "SKILLS_WRITABLE",
    "SELF_CONTEXT_WRITABLE",
    # Bounded owner-facts doc (USER.md-equivalent): safe on a single-user CLI
    # (own tenant, quarantine-then-promote); multi-tenant server stays OFF.
    "OWNER_DOC_WRITABLE",
    "BACKGROUND_REVIEW_ENABLED",
    "GOALS_ENABLED",
    "CURATOR_ENABLED",
    # C4 (2026-07-11): mechanical note consolidation on the curator tick — archive
    # never-read agent-authored notes + collapse exact duplicates. Safe on a
    # single-user CLI (own tenant, archive-only, audited); server stays OFF.
    "KNOWLEDGE_CURATOR_ENABLED",
    "INSIGHTS_TOOL",
    "CODING_TOOLS_ENABLED",
    # P0-D: structured git over the confined workspace. Safe on a single-user CLI
    # (own repo); multi-tenant server stays OFF by default. git_push is separately
    # approval-gated + leaf-blocked (Task 9).
    "GIT_TOOLS_ENABLED",
    # NOTE (FL-D9): SKILL_CATALOG_INCLUDE_ALL was here, but its resolver
    # (skill_catalog_include_all(), below) hardcodes `_bool_env("SKILL_CATALOG_INCLUDE_ALL",
    # True)` directly and never consults `_safe_autonomy_default`/this set — the entry
    # was dead (default is already ON everywhere). Removed 2026-07 (behavior-neutral).
    # KB (knowledge-base) feature: safe on a single-user CLI (read/write own KB),
    # multi-tenant default stays OFF until per-tenant isolation is verified.
    "KB_ENABLED",
    # C1: context-reference expansion (@file/@folder/@diff/@url). Safe on a single-user
    # CLI where the workspace is trusted; multi-tenant server stays OFF by default so
    # accidental file-inclusion from a shared workspace is not the default.
    "CONTEXT_REFERENCES_ENABLED",
    # C9: auto-load CLAUDE.md/AGENTS.md/.cursorrules as a PROJECT_CONTEXT foundation
    # message. Safe on a single-user CLI (reads from cwd/git-root); server stays OFF
    # (multi-tenant workspaces may not have a project context file).
    "PROJECT_CONTEXT_AUTOLOAD",
    # T13: KB auto-prefetch — inject KB recall alongside memory recall at step start.
    # Safe on a single-user CLI (reads own KB); multi-tenant server stays OFF by default
    # because KB_ENABLED itself is also local-only by default.
    "KB_AUTO_PREFETCH",
    # Task 2/3/4: episodic activity ledger — durable per-run provenance rows +
    # digest injection + continuity bridge. Safe on a single-user CLI (own
    # tenant); multi-tenant server stays OFF by default.
    "EPISODIC_MEMORY_ENABLED",
    "EPISODIC_DIGEST_INJECT",
    "CONTINUITY_BRIDGE_ENABLED",
    # AU-F1.1: the goal-board dispatcher ticks under POLYROB_LOCAL (GOALS_ENABLED is
    # in this set), but without the planner nothing ever proposes an objective's next
    # goal -- the board sits idle even though the ticker runs (the "idle since Jul 1"
    # incident). Safe on a single-user CLI (own tenant's own objectives); multi-tenant
    # server stays OFF by default. Existing gates (GOALS_ENABLED, an active objective,
    # a thin ready-queue, the planner cooldown) still apply regardless of this default.
    "GOAL_PLANNER_ENABLED",
    # §7.1: self-evolution transparency — proactively notify the owner of a pending
    # identity/skill proposal + expose the approve/reject/list surface. Safe on a
    # single-user CLI (own tenant); multi-tenant server stays OFF by default (an
    # unsolicited push to a shared owner channel is opt-in there).
    "SELF_EVOLUTION_TRANSPARENCY",
    # Task 5: gated `message` action (owner/allowlist -> MessageRouter send). Safe
    # on a single-user CLI (own tenant, own owner-bound targets); multi-tenant
    # server stays OFF by default (arbitrary outbound send is opt-in there).
    "MESSAGE_TOOL_ENABLED",
    # I-6: read-only `agent_status` introspection action (steps/tools/context/
    # wallet+ledger). Safe on a single-user CLI (reads only its own runtime
    # state + own tenant's ledger); multi-tenant server stays OFF by default.
    "AGENT_STATUS_TOOL",
    # I-3 / H3 (D1): verify-before-done nudge. Safe on a single-user CLI
    # (own workspace/tests, bounded to 2 nudges, never hard-blocks done());
    # multi-tenant server stays OFF by default.
    "VERIFY_BEFORE_DONE",
    # owner-UX P2 T2: agent-callable `preferences` action (list/get/set/
    # contract_propose over the typed per-user prefs schema). Safe on a
    # single-user CLI (own tenant, safe keys write immediately, guarded keys
    # still quarantine to a pending proposal); multi-tenant server stays OFF
    # by default.
    "PREFS_TOOL_ENABLED",
    # Task 6 (Phase 1): render a branded PNG invoice card alongside the
    # text-only x402_request result (modules/pfp/cards.py). Purely a
    # presentation nicety over an already-created invoice (fail-open, never
    # blocks the request) — safe on a single-user CLI; multi-tenant server
    # stays OFF by default (extra render cost/surface per invoice is opt-in
    # there).
    "INVOICE_CARD_ENABLED",
})


def local_mode_enabled() -> bool:
    """True when running as the single-user terminal-native agent.

    Canonical flag: ``POLYROB_LOCAL``. ``ROB_LOCAL`` is accepted as a deprecated
    back-compat alias (older docs/scripts referenced it) — either being truthy
    enables local mode, so a doc that still says ``ROB_LOCAL`` isn't a silent no-op.
    """
    return _bool_env("POLYROB_LOCAL", False) or _bool_env("ROB_LOCAL", False)


def message_tool_enabled() -> bool:
    """Whether the gated `message` action (owner/allowlist -> MessageRouter) is
    registered. Default OFF; ON under POLYROB_LOCAL (single-user CLI) via the
    _SAFE_LOCAL_FLAGS group. An explicit MESSAGE_TOOL_ENABLED always wins.
    """
    return _bool_env("MESSAGE_TOOL_ENABLED", _safe_autonomy_default("MESSAGE_TOOL_ENABLED"))


def message_autonomous_allowlisted() -> bool:
    """Whether an autonomous/forged turn (goal/cron/planner session, sub-agent,
    self-wake re-entry) may use the `message` action AT ALL. Default OFF =
    blanket refusal ("owner must be in the loop"). ON = the send falls through
    to the normal target-tier gate, so it can reach ONLY the owner or an
    owner-ALLOWLISTED target (`polyrob owner allow <surface> <target>` /
    `/allow` in chat) — the allowlist is the owner-in-the-loop mechanism.
    Battle-test kickoff 2026-07-14: the owner sanctions autonomous posting to
    the allowlisted promo chats; multi-tenant server default stays OFF.
    Proposal 013 (T2): ON under effective AUTONOMY_MODE=autonomous (single-owner
    instance) via _mode_capability_default; an explicit env always wins.
    """
    return _bool_env("MESSAGE_AUTONOMOUS_ALLOWLISTED",
                      _mode_capability_default("MESSAGE_AUTONOMOUS_ALLOWLISTED"))


def prefs_tool_enabled() -> bool:
    """Whether the agent-callable `preferences` action (owner-UX P2 T2) is
    registered. Default OFF; ON under POLYROB_LOCAL (single-user CLI) via the
    _SAFE_LOCAL_FLAGS group. An explicit PREFS_TOOL_ENABLED always wins.

    Gates the whole action, not the pref schema itself (core.prefs.prefs_enabled()
    is the independent SSOT for whether preferences.toml is consulted at all —
    this only controls whether the agent can call the tool to read/change one).
    """
    return _bool_env("PREFS_TOOL_ENABLED", _safe_autonomy_default("PREFS_TOOL_ENABLED"))


def invoice_card_enabled() -> bool:
    """Whether `x402_request` also renders a branded PNG invoice card into the
    session workspace (`modules/pfp/cards.py::render_invoice_card`) alongside
    the existing text-only result. Default OFF; ON under POLYROB_LOCAL
    (single-user CLI) via the _SAFE_LOCAL_FLAGS group. An explicit
    INVOICE_CARD_ENABLED always wins.

    Render failures are fail-open regardless of this flag —
    `tools/x402/invoice_tool.py` catches any card-render error, logs one WARN,
    and returns the text-only result unchanged (the invoice itself already
    succeeded by the time this is consulted).
    """
    return _bool_env("INVOICE_CARD_ENABLED", _safe_autonomy_default("INVOICE_CARD_ENABLED"))


def eip8004_payment_feedback_enabled() -> bool:
    """Task 15 (Phase 4): settlement -> ERC-8004 payment-backed reputation
    hook (`SettlementWatcher._maybe_offer_payment_feedback`) — on settlement
    of an invoice with an identifiable payer (`correspondent_ref`) and a
    verifiable on-chain transaction, offer the payer a signed
    FeedbackAuthorization + ProofOfPayment they can redeem later. Never
    auto-submits feedback on the payer's behalf (that would be fake
    reputation) — this only gates the OFFER.

    Rides `EIP8004_ENABLED` — checked the SAME way `get_eip8004_config()` /
    `require_eip8004_enabled()` do (strict `'true'` string match, not the
    broader `bool_env` falsey-set) so this flag can never disagree with the
    rest of the EIP-8004 module about whether ERC-8004 itself is on. Default
    OFF (both must be explicitly enabled).
    """
    if os.environ.get("EIP8004_ENABLED", "false").lower() != "true":
        return False
    return _bool_env("EIP8004_PAYMENT_FEEDBACK", False)


# --- AUTONOMY_MODE — the capability/approval master switch (proposal 013) -----------
#
# A FOURTH axis, reconciled with the existing three: POLYROB_LOCAL = trust profile,
# AUTONOMY_POSTURE = which autonomy loops run (its DEFAULT is raised to `full` by this
# mode), AGENT_COMPUTE_POSTURE = host capability (NOT touched by this mode).
#
#   supervised (default) — today's behavior, byte-identical. Deny-by-default gates.
#   autonomous           — single-owner act-and-report instance: capability flags below
#                          default ON, autonomous toolset defaults to the full non-money
#                          set, approvals default to allow+audit+notify, outbound
#                          defaults to policy `open` with caps. Money-SPEND, secrets and
#                          host access are NEVER moved by this mode.
#
# Activation is guarded: `autonomous` is only effective on a single-owner deployment
# (local mode + a bound owner principal); otherwise it clamps to supervised with a
# one-time WARN, so a multi-tenant server can never drift into it.
_AUTONOMY_MODES = ("supervised", "autonomous")

_MODE_CAPABILITY_FLAGS = frozenset({
    "TWITTER_ENABLED",
    "MCP_ENABLED",
    "GROUP_CHAT_ENABLED",
    "EMAIL_SURFACE_ENABLED",
    "X402_INVOICE_ENABLED",           # RECEIVE side only; x402_pay/wallet stay OFF
    "MESSAGE_AUTONOMOUS_ALLOWLISTED",
    "CORRESPONDENT_ACCESS_ENABLED",   # substrate for outbound-policy rails (T5/T6)
    "CORRESPONDENT_REPLY_ENABLED",
})

_FULL_AUTONOMY_WARNED = False


def autonomy_mode() -> str:
    """Resolved AUTONOMY_MODE (supervised|autonomous). Default `supervised` —
    byte-identical to pre-013 behavior. Unknown values degrade to supervised so a
    typo never activates the capable posture. Access-time (tests/bootstrap see env)."""
    raw = (os.getenv("AUTONOMY_MODE") or "").strip().lower()
    return raw if raw in _AUTONOMY_MODES else "supervised"


def full_autonomy_enabled() -> bool:
    """True only when the operator set AUTONOMY_MODE=autonomous AND this deployment is
    a single-owner instance (local mode + a bound owner principal). Anything else
    clamps to supervised semantics with a one-time WARN — multi-tenant conservatism is
    by construction, not convention."""
    global _FULL_AUTONOMY_WARNED
    if autonomy_mode() != "autonomous":
        return False
    reason = None
    if not local_mode_enabled():
        reason = "POLYROB_LOCAL is not set (multi-tenant/server deployment)"
    else:
        try:
            from core.instance import (
                resolve_owner_email,
                resolve_owner_principal,
                resolve_owner_telegram_id,
            )
            owner_bound = (
                resolve_owner_principal(default_to_instance=False) is not None
                or bool(resolve_owner_telegram_id())
                or bool(resolve_owner_email())
            )
            if not owner_bound:
                reason = "no owner principal is bound (POLYROB_OWNER_USER_ID/…)"
        except Exception as e:  # never let the guard itself crash a resolver
            reason = f"owner resolution failed: {e}"
    if reason:
        if not _FULL_AUTONOMY_WARNED:
            logging.getLogger(__name__).warning(
                "AUTONOMY_MODE=autonomous requested but %s — clamping to supervised", reason)
            _FULL_AUTONOMY_WARNED = True
        return False
    return True


def autonomy_mode_display() -> str:
    """One-line human display of the resolved autonomy mode (T10 control-plane
    visibility — Telegram `/status`, `polyrob owner show`, `polyrob doctor`).

    Three exact strings:
      - ``"supervised"`` — AUTONOMY_MODE unset/supervised (today's default).
      - ``"autonomous (effective)"`` — AUTONOMY_MODE=autonomous AND the
        single-owner guard actually granted it (:func:`full_autonomy_enabled`).
      - ``"autonomous (clamped — needs POLYROB_LOCAL + owner binding)"`` — the
        operator requested autonomous but the guard clamped it back to
        supervised (multi-tenant deployment or no bound owner).
    """
    if autonomy_mode() != "autonomous":
        return "supervised"
    if full_autonomy_enabled():
        return "autonomous (effective)"
    return "autonomous (clamped — needs POLYROB_LOCAL + owner binding)"


def _mode_capability_default(flag_name: str) -> bool:
    """Default for a mode-governed capability flag: ON under effective autonomous
    mode, else OFF. Explicit per-flag env ALWAYS wins at the call site (this is only
    the default argument to _bool_env/bool_env)."""
    return full_autonomy_enabled() and flag_name in _MODE_CAPABILITY_FLAGS


# Task 9 (G-2): outward-facing payment-CREATION actions — gated by PAYMENT_APPROVAL_MODE
# regardless of the generic APPROVAL_REQUIRED_TOOLS opt-in (payment gating is first-class,
# not opt-in). A future subscription-renewal verb joins this tuple, not a new mode.
PAYMENT_APPROVAL_TOOLS = (
    "x402_request",
    # L9 (2026-07-15): live-trade order verbs are money-moving too — a within-cap
    # live order should get the SAME owner-in-the-loop an invoice does, not slip
    # through unattended. Container-tool actions register NAMESPACED
    # (tools/controller/tool_management.py), so these namespaced names are the
    # runtime action names the payment-approval gate matches.
    "hyperliquid_place_limit_order",
    "hyperliquid_place_market_order",
    "polymarket_place_limit_order",
    "polymarket_place_market_order",
)

# 013 T7 review (Important finding fix): PAYMENT_APPROVAL_TOOLS is NOT one uniform
# lane. This tuple carves out the RECEIVE-side subset that is eligible for
# act-and-report under PAYMENT_APPROVAL_MODE=auto (post-hoc owner notify only, no
# pre-approval block) — today just the invoicing verb. Every OTHER entry in
# PAYMENT_APPROVAL_TOOLS (the live-trade order verbs above) is treated as
# SPEND-side and ALWAYS keeps owner_queue pre-approval regardless of mode — see
# tools/controller/service.py's `_spend_tools = _payment_tools - set(this tuple)`
# wiring. This is a fail-safe by construction: a future addition to
# PAYMENT_APPROVAL_TOOLS that is NOT also added here defaults to the strict
# (pre-approved) lane, never silently to act-and-report. The hard product line
# (proposal 013) is money-spend/trading is NEVER act-and-report, even under an
# explicit PAYMENT_APPROVAL_MODE=auto.
PAYMENT_RECEIVE_APPROVAL_TOOLS = (
    "x402_request",
)


# fix pass 1 (Finding 2): the payment-approval flags are FROZEN AT IMPORT — snapshotted
# once, exactly like APPROVAL_REQUIRED_TOOLS/APPROVAL_PROVIDER are in
# `tools/controller/approval.py` (WS-7) — so a mid-process env mutation (e.g. a
# prompt-injected write to the process env, or a config-reload race) can never retarget
# money-critical gating (queue-vs-auto mode, the owner-response wait, or the one-shot
# grant TTL) mid-session. Operators set these in real process env at startup.
#
# 013 T7: the default (unset/invalid PAYMENT_APPROVAL_MODE) is MODE-DEPENDENT —
# supervised (default) keeps "approve" (byte-identical); under full autonomous mode
# it defaults to "auto", because the "approve" -> owner_queue path hard-denies
# forged/autonomous turns (`OwnerQueueApprover`, tools/controller/approval_queue.py),
# making autonomous invoicing impossible under "approve". "auto" still executes only
# within X402_INVOICE_MAX_USD/X402_INVOICE_DAILY_MAX and fires a post-hoc owner
# notify for every within-cap creation of a RECEIVE-side verb
# (PAYMENT_RECEIVE_APPROVAL_TOOLS). An explicit PAYMENT_APPROVAL_MODE always wins
# in both modes.
#
# T7 review (Important finding fix): PAYMENT_APPROVAL_TOOLS also holds SPEND-side
# live-trade order verbs (hyperliquid/polymarket, L9) — mode="auto" does NOT
# loosen pre-approval for those. Every entry in PAYMENT_APPROVAL_TOOLS that is
# NOT in PAYMENT_RECEIVE_APPROVAL_TOOLS is SPEND-side and keeps owner_queue
# pre-approval under BOTH "approve" and "auto" (see
# tools/controller/service.py::Controller.__init__ payment-approval wiring). This
# protection is unconditional — it also applies under an EXPLICIT
# PAYMENT_APPROVAL_MODE=auto, which is a deliberate behavior change vs the prior
# (T7-only) cut for any deployment that had set PAYMENT_APPROVAL_MODE=auto
# explicitly: previously that setting also act-and-reported trade verbs; it no
# longer does. The hard product line (proposal 013): money-SPEND/trading stays
# deny-by-default / owner-in-the-loop and is never act-and-report, in any mode.
def _snapshot_payment_approval_mode() -> str:
    raw = (os.getenv("PAYMENT_APPROVAL_MODE") or "").strip().lower()
    if raw in ("approve", "auto"):
        return raw
    return "auto" if full_autonomy_enabled() else "approve"


def _snapshot_payment_approval_timeout_sec() -> float:
    raw = os.getenv("APPROVAL_TIMEOUT_SEC")
    if raw is None or not raw.strip():
        return 300.0
    try:
        return float(raw)
    except ValueError:
        return 300.0


def _snapshot_approval_grant_ttl_hours() -> float:
    try:
        return float(os.getenv("APPROVAL_GRANT_TTL_HOURS", "24"))
    except ValueError:
        return 24.0


_FROZEN_PAYMENT_APPROVAL_MODE = _snapshot_payment_approval_mode()
_FROZEN_PAYMENT_APPROVAL_TIMEOUT_SEC = _snapshot_payment_approval_timeout_sec()
_FROZEN_APPROVAL_GRANT_TTL_HOURS = _snapshot_approval_grant_ttl_hours()


def payment_approval_mode() -> str:
    """PAYMENT_APPROVAL_MODE — the owner-legible switch for outward payment requests
    (`tools/controller/service.py` wires this against :data:`PAYMENT_APPROVAL_TOOLS`,
    split into :data:`PAYMENT_RECEIVE_APPROVAL_TOOLS` vs the SPEND-side remainder):

      - ``"approve"``: every request (receive AND spend) queues through the durable,
        remote-capable ``owner_queue`` provider (`tools/controller/approval_queue.py`)
        — a real owner tap that works over Telegram even though prod Rob is headless
        (closes G-2: the only approver used to be a blocking stdin prompt).
      - ``"auto"``: the RECEIVE-side subset (``x402_request``) is NOT queued — it
        executes immediately (still bounded by `modules/x402/invoicing.py`'s own
        per-invoice/per-day caps, never duplicated here), and a post-execution owner
        notification + audit event fires for every within-cap creation. The SPEND-side
        subset (live-trade order verbs) is UNAFFECTED by this mode — it still queues
        through ``owner_queue`` exactly as under ``"approve"``, because trading is
        never act-and-report (see :data:`PAYMENT_RECEIVE_APPROVAL_TOOLS`).

    An explicit env value (typo aside) always wins. When ``PAYMENT_APPROVAL_MODE`` is
    unset (or an invalid value), the DEFAULT is mode-dependent (013 T7): ``"approve"``
    under supervised mode (byte-identical to pre-013), ``"auto"`` under full autonomous
    mode (`full_autonomy_enabled()`) — because ``"approve"``'s owner_queue path
    hard-denies forged/autonomous turns, which would otherwise make receive-side
    invoicing impossible for a single-owner autonomous instance. Regardless of default
    vs explicit, ``"auto"`` only ever act-and-reports the RECEIVE-side subset — money-
    SPEND (x402_pay, wallet, trading order-placement) always keeps owner-in-the-loop
    pre-approval, in EVERY mode (013 T7 review fix, closing an Important finding: the
    original T7 cut let an explicit or mode-defaulted ``"auto"`` silently drop
    pre-approval for the live-trade verbs too).

    FROZEN AT IMPORT (fix pass 1 / Finding 2) — see :func:`_refreeze_payment_approval_flags_for_tests`
    for the test-only re-snapshot seam.
    """
    return _FROZEN_PAYMENT_APPROVAL_MODE


def payment_approval_timeout_sec() -> float:
    """``APPROVAL_TIMEOUT_SEC`` for payment-creation actions specifically — reuses
    the SAME env var as the generic approval seam
    (`tools/controller/approval.py::DEFAULT_APPROVAL_TIMEOUT_SEC`, default 30s), but
    an owner_queue wait defaults to a money-appropriate **300s**: a real owner
    round-trip over Telegram needs minutes, not seconds, and 30s would time out
    almost every legitimate approval. An operator who explicitly sets
    ``APPROVAL_TIMEOUT_SEC`` still wins for BOTH the generic and the payment seam —
    no new flag.

    FROZEN AT IMPORT (fix pass 1 / Finding 2) — see :func:`_refreeze_payment_approval_flags_for_tests`.
    """
    return _FROZEN_PAYMENT_APPROVAL_TIMEOUT_SEC


def approval_grant_ttl_hours() -> float:
    """TTL (hours) for a ``owner_queue`` ONE-SHOT grant: an owner decision recorded
    AFTER the requester already timed out and gave up still lets the NEXT identical
    request (same tool + params + tenant) through without re-queuing — but only within
    this window, so a decision from days ago can't silently auto-approve a fresh replay.

    FROZEN AT IMPORT (fix pass 1 / Finding 2) — see :func:`_refreeze_payment_approval_flags_for_tests`.
    """
    return _FROZEN_APPROVAL_GRANT_TTL_HOURS


def _refreeze_payment_approval_flags_for_tests() -> None:
    """TEST-ONLY: re-snapshot the payment-approval flags from the current env.

    Mirrors `tools.controller.approval._refreeze_approval_flags_for_tests` — production
    never calls this. A test that mutates ``PAYMENT_APPROVAL_MODE`` /
    ``APPROVAL_TIMEOUT_SEC`` / ``APPROVAL_GRANT_TTL_HOURS`` via ``monkeypatch`` must call
    this AFTER setting the env (and again in teardown) for the frozen helpers above to
    observe the change — by design, a plain env mutation does NOT flip them.
    """
    global _FROZEN_PAYMENT_APPROVAL_MODE, _FROZEN_PAYMENT_APPROVAL_TIMEOUT_SEC, \
        _FROZEN_APPROVAL_GRANT_TTL_HOURS
    _FROZEN_PAYMENT_APPROVAL_MODE = _snapshot_payment_approval_mode()
    _FROZEN_PAYMENT_APPROVAL_TIMEOUT_SEC = _snapshot_payment_approval_timeout_sec()
    _FROZEN_APPROVAL_GRANT_TTL_HOURS = _snapshot_approval_grant_ttl_hours()


def task_personality_block_enabled() -> bool:
    """Whether the persona/<identity> block is injected into the task agent.

    S1 (chat consolidation): injects the chat agent's character/personality into
    the unified Task agent's <identity> block so chat-mode carries persona without
    a separate ChatAgent. The persona TEXT is rendered from a Character via the
    pure agents/personality/persona_render.render_persona_block; the task-agent
    core only ever sees a str (it never imports the chat stack). This access-time
    gate is the live seam — the module-level TASK_PERSONALITY_BLOCK constant that
    used to exist was a dead decoy.

    Access-time (not import-time) so it sees POLYROB_LOCAL set via
    os.environ.setdefault in bootstrap. Defaults ON under local mode (single-user
    CLI wants its persona; persona_block resolves to "" when OFF => byte-identical
    system prompt), OFF on the multi-tenant server. An explicit
    TASK_PERSONALITY_BLOCK env always wins.
    """
    raw = os.getenv("TASK_PERSONALITY_BLOCK")
    if raw is None or raw.strip() == "":
        return local_mode_enabled()
    return _bool_env("TASK_PERSONALITY_BLOCK", False)


def memory_prefetch_cadence(autonomous: bool = False) -> int:
    """Steps between automatic memory re-prefetch (Phase 1.3).

    0 = prefetch on the FIRST step only (legacy, prod-safe). N>0 = ALSO prefetch
    every N steps so a long task keeps re-recalling phase-relevant memory instead of
    recalling once at step 1 and never again.

    Resolved at ACCESS time (not import) so it sees POLYROB_LOCAL even though that is
    set via os.environ.setdefault in bootstrap, which may run after this module is
    first imported. Defaults to 3 under local mode AND for an autonomous session
    (SA-06 — a server goal/cron run otherwise recalled once at step 1, where the
    brain enrichment is dead, and never again); 0 for server chat. An explicit
    ``MEMORY_PREFETCH_CADENCE`` (incl. ``0``) always wins.
    """
    return _int_env("MEMORY_PREFETCH_CADENCE",
                    3 if (local_mode_enabled() or autonomous) else 0)


def hmem_tail_placement() -> bool:
    """Whether in-session hierarchical memory is placed as a dynamic SUFFIX after the
    conversation (Phase 0.1) instead of in the foundation ahead of it.

    The H-MEM block changes every step; in the foundation prefix it invalidated the
    prompt cache for everything after it (skills tail + all conversation) on every
    step. As a tail suffix, the stable foundation + growing conversation form a
    cacheable prefix and only the small H-MEM suffix is reprocessed.

    Resolved at access time. Default ON everywhere (T1-09, 2026-07-06): it soaked
    locally since 2026-06 with no regressions, while the OFF server default broke the
    server prompt cache every step. Explicit ``HMEM_TAIL_PLACEMENT=false`` restores
    the legacy foundation placement.
    """
    return _bool_env("HMEM_TAIL_PLACEMENT", True)


def ticker_idle_backoff_enabled() -> bool:
    """Whether idle background tickers (cron, goal dispatch) back off their poll
    interval when a tick finds no due work, instead of firing at a fixed cadence
    forever.

    A fixed 60s ticker costs nothing on a multi-tenant server with steady job
    volume, but on a single-user local CLI (POLYROB_LOCAL) it is close to the
    only thing keeping the process from ever going idle -- a real contributor
    to laptop battery drain. Backoff only kicks in on demonstrably idle ticks
    (nothing ran, nothing failed) and resets to the base interval the moment
    work resumes, so responsiveness is unaffected once something is actually
    happening.

    Resolved at access time. Defaults ON under POLYROB_LOCAL, OFF on the
    multi-tenant server (byte-identical fixed-cadence legacy) so a precisely
    time-scheduled cron job on a shared server never slips. Explicit
    ``TICKER_IDLE_BACKOFF_ENABLED`` always wins.
    """
    return _bool_env("TICKER_IDLE_BACKOFF_ENABLED", local_mode_enabled())


def ticker_idle_backoff_max_multiplier() -> int:
    """Cap on how many multiples of a ticker's base interval an idle backoff may
    reach (e.g. 5x a 60s base = 300s = 5 minutes worst-case staleness before a
    newly-due job is noticed). Explicit ``TICKER_IDLE_BACKOFF_MAX_MULTIPLIER``
    always wins; default 5.
    """
    return _int_env("TICKER_IDLE_BACKOFF_MAX_MULTIPLIER", 5)


def embedder_needed() -> bool:
    """Whether this deployment actually needs the sentence-transformers embedder (torch).

    SSOT for both the CLI (maybe_register_cli_embedder) and the server (initialize_modules):
    only build the heavy embedder when KB is enabled, MEMORY_BACKEND=local_vector (hybrid
    vector recall), or local mode. The default MEMORY_BACKEND=sqlite uses FTS5 keyword recall
    and needs no embeddings. See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md (P1-EMB).
    """
    return (
        AutonomyConfig.kb_enabled()
        or os.getenv("MEMORY_BACKEND", "sqlite").lower() == "local_vector"
        or local_mode_enabled()
    )


def _safe_autonomy_default(flag_name: str) -> bool:
    """Default for a safe autonomy flag: ON under local mode, else OFF."""
    return local_mode_enabled() if flag_name in _SAFE_LOCAL_FLAGS else False


# --- W1-1: AUTONOMY_POSTURE — one coherent switch for the shipped-but-dark loops ----
#
# Five autonomy flags shipped wired but default-OFF in BOTH modes, each behind its own
# env var, so making an instance actually verify + report its autonomous work meant
# flipping five independent flags with no single lever (the "activation, not machinery"
# gap). AUTONOMY_POSTURE is a second axis (orthogonal to _SAFE_LOCAL_FLAGS) that moves
# the DEFAULTS of that group together. An explicit per-flag env ALWAYS wins (only the
# default moves), and the unset/`silent` posture is byte-identical to today.
#
#   silent        (default) — today's behavior: autonomy runs but is unverified + silent.
#   owner-visible — the agent's autonomous work becomes VERIFIED + owner-visible:
#                   completion judge, blocker->owner escalation (+ ask), self-wake
#                   delivery, continuity bridge. Safe for single-user local; on a
#                   multi-tenant server it is an opt-in (unsolicited pushes / aux cost).
#   full          — owner-visible PLUS time-based initiative (cron ticker).
_POSTURE_OWNER_VISIBLE_FLAGS = frozenset({
    "GOAL_COMPLETION_JUDGE",
    "GOAL_BLOCKER_ESCALATION",
    "GOAL_SELF_WAKE_ENABLED",
    "AUTONOMOUS_CONTINUITY_BRIDGE",
    # Continuity/learning trio: ON under the local profile, and an owner-visible
    # posture also turns it on server-side (memory + verification, no
    # unsolicited-cost initiative). Explicit env wins.
    "EPISODIC_MEMORY_ENABLED",
    "EPISODIC_DIGEST_INJECT",
    "REFLECTION_ON_SESSION_CLOSE",
})
_POSTURE_FULL_FLAGS = _POSTURE_OWNER_VISIBLE_FLAGS | {
    "CRON_ENABLED", "WAKE_CHANGE_GATE", "AUTONOMY_START_NOTICE",
}
_AUTONOMY_POSTURES = ("silent", "owner-visible", "full")


def autonomy_posture() -> str:
    """Resolved AUTONOMY_POSTURE (silent|owner-visible|full).

    Default `silent` in BOTH modes (byte-identical to pre-W1-1). An unknown value
    degrades to `silent` so a typo never silently activates autonomy. Access-time so
    tests/bootstrap env changes are seen.
    """
    raw = (os.getenv("AUTONOMY_POSTURE") or "").strip().lower()
    if raw in _AUTONOMY_POSTURES:
        return raw
    return "full" if full_autonomy_enabled() else "silent"


def _posture_autonomy_default(flag_name: str) -> bool:
    """Default for a posture-governed autonomy flag, given the resolved posture."""
    posture = autonomy_posture()
    if posture == "full":
        return flag_name in _POSTURE_FULL_FLAGS
    if posture == "owner-visible":
        return flag_name in _POSTURE_OWNER_VISIBLE_FLAGS
    return False  # silent: today's defaults


# --- AGENT_COMPUTE_POSTURE — the compute-capability ladder (computer-use parity) ----
#
# A THIRD capability axis, orthogonal to POLYROB_LOCAL (single-user trust profile)
# and AUTONOMY_POSTURE (which autonomy loops run): how much host/compute capability
# the agent has.
#
#   0  confined      (default) — today's docker sandbox, no persistent shell.
#   1  sandbox-dev   — persistent networked sandbox + importable pip installs +
#                      `shell` scoped INTO the container + loopback port-publish.
#   2  self-maintain — posture 1 + the approval-gated `self_env` verbs.
#   3  host          — full host access; requires POLYROB_LOCAL and a
#                      single-tenant box (refused on network-facing surfaces).
#
# SECURITY CONTRACT:
# - Default CLOSED: unset/garbage/out-of-range -> 0. Garbage NEVER rounds up —
#   only the literal values 0|1|2|3 are accepted (a typo'd "9" must not grant the
#   host tier).
# - FROZEN AT IMPORT: the value is snapshotted once at module import so a
#   mid-process env mutation (e.g. a prompt-injected write that reached an
#   env-mutating surface) can never raise the running posture. Operators set it
#   in real process env (systemd EnvironmentFile / shell / dotenv loaded at
#   process start, see main.py) — never at runtime.
# - The posture is the "may" side only; the existing correspondent-taint gate and
#   delegation blocklist stay the "never" side. `compute_posture_allows` is the
#   ONE predicate every posture-gated capability must call.

def _resolve_compute_posture(raw) -> int:
    """PURE: parse one env value -> posture int. Only literal 0|1|2|3 accepted;
    anything else (unset/garbage/out-of-range) degrades CLOSED to 0."""
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return val if val in (0, 1, 2, 3) else 0


_COMPUTE_POSTURE_FROZEN = _resolve_compute_posture(os.getenv("AGENT_COMPUTE_POSTURE"))


def compute_posture() -> int:
    """Resolved AGENT_COMPUTE_POSTURE (0-3), FROZEN at import (see block comment)."""
    return _COMPUTE_POSTURE_FROZEN


def _refreeze_compute_posture_for_tests() -> int:
    """TEST-ONLY seam: re-snapshot the frozen posture from the current env.

    Production code must never call this — the freeze is the security property.
    """
    global _COMPUTE_POSTURE_FROZEN
    _COMPUTE_POSTURE_FROZEN = _resolve_compute_posture(os.getenv("AGENT_COMPUTE_POSTURE"))
    return _COMPUTE_POSTURE_FROZEN


def compute_posture_allows(execution_context, min_posture: int) -> bool:
    """THE single gate predicate for posture-gated compute capabilities.

    True only when ALL hold:
      (a) frozen ``compute_posture() >= min_posture``;
      (b) the session tenant is the OWNER — ``is_owner_local_safe`` (principal
          match always wins; the POLYROB_LOCAL bypass is honored ONLY for the
          CLI's ``local`` operator tenant, so a forgeable network sender under
          POLYROB_LOCAL=1 on a public surface is never auto-owned);
      (c) not a leaf/sub-agent context;
      (d) not a forged self-wake/delegation-result re-entry turn (the
          ``metadata["turn_kind"]`` stamp, SK-F10).

    An autonomous goal/cron session of the owner tenant PASSES (no forged stamp;
    role is orchestrator) — deliberate: WS-8 provisions those runs with the
    compute toolset, and the correspondent-taint gate + delegation blocklist
    remain the independent "never" side. ``min_posture <= 0`` is the
    unconditional baseline (posture-0 capabilities keep their own gates).
    Fail-CLOSED: any fault in resolution denies.
    """
    if min_posture <= 0:
        return True
    try:
        if compute_posture() < min_posture:
            return False
        if execution_context is None:
            return False
        if getattr(execution_context, "is_sub_agent", False):
            return False
        if getattr(execution_context, "role", "leaf") != "orchestrator":
            return False
        metadata = getattr(execution_context, "metadata", None) or {}
        # R-4: the forged-turn kinds SSOT is core-tier now — no fallback needed.
        from core.security.forged_turns import FORGED_TURN_KINDS as _kinds
        if metadata.get("turn_kind") in _kinds:
            return False
        from core.instance import is_owner_local_safe, resolve_owner_principal
        return is_owner_local_safe(
            getattr(execution_context, "user_id", None),
            owner_principal=resolve_owner_principal(),
            local_enabled=local_mode_enabled(),
        )
    except Exception:
        return False  # fail-closed: can't prove entitlement -> deny


class AutonomyConfig:
    """Feature flags + caps for the autonomy/continuous-learning loops.

    Read through this class (not raw os.getenv) so every loop shares one parser and
    the defaults are documented in one place. Evaluated at access time (classmethod /
    property-free) so tests can monkeypatch env between calls.
    """

    # W1 — self-wake rail
    @staticmethod
    def self_wake_enabled() -> bool:
        return _bool_env("SELF_WAKE_ENABLED", _safe_autonomy_default("SELF_WAKE_ENABLED"))

    @staticmethod
    def self_wake_max_reentries() -> int:
        return _int_env("SELF_WAKE_MAX_REENTRIES", 3)

    @staticmethod
    def self_wake_idle_backoff_sec() -> float:
        try:
            return float(os.getenv("SELF_WAKE_IDLE_BACKOFF_SEC", "30"))
        except (TypeError, ValueError):
            return 30.0

    # W2 — writable skills + background review
    @staticmethod
    def skills_writable() -> bool:
        return _bool_env("SKILLS_WRITABLE", _safe_autonomy_default("SKILLS_WRITABLE"))

    @staticmethod
    def skills_writable_require_review() -> bool:
        return _bool_env("SKILLS_WRITABLE_REQUIRE_REVIEW", True)

    @staticmethod
    def skill_overwrite_protect() -> bool:
        # An agent/background overwrite of an existing ACTIVE skill becomes a .pending
        # proposal (owner promotes); all overwrites archive the prior body. Default ON.
        return _bool_env("SKILL_OVERWRITE_PROTECT", True)

    # polyrob C-write — evolving SELF identity (agent-writable per-(instance,user) doc)
    @staticmethod
    def self_context_writable() -> bool:
        return _bool_env("SELF_CONTEXT_WRITABLE", _safe_autonomy_default("SELF_CONTEXT_WRITABLE"))

    @staticmethod
    def self_context_require_review() -> bool:
        return _bool_env("SELF_CONTEXT_REQUIRE_REVIEW", True)

    # Bounded owner-facts doc (USER.md-equivalent) — agent-maintained per-(instance,
    # user) document of durable owner facts/preferences, injected on the SELF/SOUL
    # seam. Same quarantine-then-promote model as SELF; ON under the local profile.
    @staticmethod
    def owner_doc_writable() -> bool:
        return _bool_env("OWNER_DOC_WRITABLE", _safe_autonomy_default("OWNER_DOC_WRITABLE"))

    @staticmethod
    def owner_doc_require_review() -> bool:
        return _bool_env("OWNER_DOC_REQUIRE_REVIEW", True)

    # Bounded operating-contract doc (owner-authored operating rules/constraints,
    # owner-UX Phase 2) — injected after owner facts and before the evolving SELF
    # doc on the SELF/SOUL seam. Same quarantine-then-promote model as the owner
    # doc / SELF doc; default ON (unlike the writable-identity flags, this is not
    # gated to the local-safe group — it's a read/inject default, and the writer
    # itself already refuses forged authors + requires review).
    @staticmethod
    def contract_doc_enabled() -> bool:
        return _bool_env("CONTRACT_DOC_ENABLED", True)

    @staticmethod
    def contract_doc_require_review() -> bool:
        return _bool_env("CONTRACT_DOC_REQUIRE_REVIEW", True)

    # §7.1 — self-evolution transparency + owner control loop
    @staticmethod
    def self_evolution_transparency() -> bool:
        return _bool_env("SELF_EVOLUTION_TRANSPARENCY",
                         _safe_autonomy_default("SELF_EVOLUTION_TRANSPARENCY"))

    @staticmethod
    def background_review_enabled() -> bool:
        return _bool_env("BACKGROUND_REVIEW_ENABLED", _safe_autonomy_default("BACKGROUND_REVIEW_ENABLED"))

    @staticmethod
    def bg_review_interval() -> int:
        return _int_env("BG_REVIEW_INTERVAL", 10)

    @staticmethod
    def bg_review_max_steps() -> int:
        return _int_env("BG_REVIEW_MAX_STEPS", 8)

    # 019 — live run-state observability: master gate for the span/wait feed
    # events (tool_started / llm_started / awaiting_approval / approval_resolved).
    # Default ON, fail-open; OFF restores the pre-019 outcome-only feed.
    @staticmethod
    def run_events_enabled() -> bool:
        return _bool_env("RUN_EVENTS_ENABLED", True)

    # 019 P2 — Telegram live progress bubble (throttled edits of the one
    # ⚙️ Working… status message). Per-owner opt-out via pref progress.telegram.
    @staticmethod
    def telegram_progress_edits() -> bool:
        return _bool_env("TELEGRAM_PROGRESS_EDITS", True)

    # 019 P2 — owner notice when an AUTONOMOUS run (goal/cron) STARTS — today
    # the owner learns only at completion/digest. Same defaulting shape as
    # WAKE_CHANGE_GATE: ON under AUTONOMY_POSTURE=full / autonomous mode.
    @staticmethod
    def autonomy_start_notice() -> bool:
        return _bool_env("AUTONOMY_START_NOTICE",
                         _posture_autonomy_default("AUTONOMY_START_NOTICE"))

    # W3 — cron run-loop + delivery
    @staticmethod
    def cron_run_loop() -> bool:
        return _bool_env("CRON_RUN_LOOP", True)

    @staticmethod
    def cron_delivery_enabled() -> bool:
        return _bool_env("CRON_DELIVERY_ENABLED", False)

    # Owner daily digest: a cron job carrying payload.digest is composed
    # deterministically ($0, no model turn) from the ledger + event log + open
    # asks and pushed via the cron delivery rail. Default OFF.
    @staticmethod
    def owner_digest_enabled() -> bool:
        return _bool_env("OWNER_DIGEST_ENABLED", False)

    # W4 — durable goal board
    @staticmethod
    def goals_enabled() -> bool:
        return _bool_env("GOALS_ENABLED", _safe_autonomy_default("GOALS_ENABLED"))

    @staticmethod
    def goal_max_retries() -> int:
        return _int_env("GOAL_MAX_RETRIES", 2)

    @staticmethod
    def goal_claim_ttl_sec() -> int:
        return _int_env("GOAL_CLAIM_TTL_SEC", 900)

    @staticmethod
    def goal_max_run_seconds() -> int:
        """H11: hard wall-clock cap on a single goal run (mirrors cron's per-job cap).
        A goal is otherwise bounded only by max_steps, so one hung step (tool/LLM/browser)
        blocks forever and permanently occupies a GOAL_MAX_CONCURRENT slot."""
        return _int_env("GOAL_MAX_RUN_SECONDS", 1800)

    @staticmethod
    def goal_dispatch_interval_sec() -> int:
        return _int_env("GOAL_DISPATCH_INTERVAL_SEC", 60)

    @staticmethod
    def goal_max_concurrent() -> int:
        return _int_env("GOAL_MAX_CONCURRENT", 2)

    @staticmethod
    def goal_dedup_threshold() -> float:
        try:
            return float(os.getenv("GOAL_DEDUP_THRESHOLD", "0.6"))
        except (TypeError, ValueError):
            return 0.6

    @staticmethod
    def goal_planner_enabled() -> bool:
        return _bool_env("GOAL_PLANNER_ENABLED", _safe_autonomy_default("GOAL_PLANNER_ENABLED"))

    @staticmethod
    def goal_planner_min_ready() -> int:
        return _int_env("GOAL_PLANNER_MIN_READY", 2)

    @staticmethod
    def goal_planner_cooldown_sec() -> int:
        return _int_env("GOAL_PLANNER_COOLDOWN_SEC", 3600)

    @staticmethod
    def goal_planner_history_n() -> int:
        return _int_env("GOAL_PLANNER_HISTORY_N", 10)

    @staticmethod
    def goal_daily_quota() -> int:
        """Max goal runs started per trailing 24h; <=0 disables the rail."""
        return _int_env("GOAL_DAILY_QUOTA", 6)

    @staticmethod
    def goal_self_wake_enabled() -> bool:
        # Was unconditional; redundant-cost finding (grok livetest 2026-06-27).
        # W1-1: default governed by AUTONOMY_POSTURE (owner-visible/full turn it on).
        return _bool_env("GOAL_SELF_WAKE_ENABLED",
                         _posture_autonomy_default("GOAL_SELF_WAKE_ENABLED"))

    @staticmethod
    def goal_notify_on_done() -> bool:
        # Tell the OWNER when a background goal COMPLETES. Default ON — the owner
        # should hear about successes, not just failures. Decoupled from
        # GOAL_SELF_WAKE_ENABLED (the agent-re-entry feature, posture-gated OFF on
        # the server): the completion push used to live only inside _self_wake, so
        # with self-wake off, completed goals told no one. This is cheap ($0, one
        # push) and controllable — set GOAL_NOTIFY_ON_DONE=false to silence.
        return _bool_env("GOAL_NOTIFY_ON_DONE", True)

    @staticmethod
    def autonomy_halted() -> bool:
        """Owner kill-switch: halt ALL autonomous dispatch + agent spend. True if
        `AUTONOMY_HALT` is set OR a halt file exists at the data home (togglable WITHOUT
        a restart — `polyrob owner halt`/`resume`, or `touch/rm <data>/AUTONOMY_HALT`).
        Per-tx/daily caps bound one payment; this stops a looping/compromised agent from
        draining via many sub-cap txs or burning budget in a stall. Fail CLOSED: a halt
        probe that cannot prove NOT-halted is treated as halted (see H6 leg 3). The
        $0/no-file/no-env default stays exactly False — the exception branch never runs
        in normal operation (os.path.exists doesn't raise for a plain path)."""
        if _bool_env("AUTONOMY_HALT", False):
            return True
        try:
            bases = [os.getenv("POLYROB_DATA_DIR"), os.getenv("DATA_ROOT")]
            # H6: also honor the RESOLVED data home (resolve_data_home) — a local
            # install's data home is cwd/.polyrob, so a halt file written there by
            # `polyrob owner halt` was previously never seen when POLYROB_DATA_DIR/
            # DATA_ROOT were unset.
            try:
                from core.runtime_paths import resolve_data_home
                bases.append(str(resolve_data_home()))
            except Exception:
                pass
            for base in bases:
                if base and os.path.exists(os.path.join(base, "AUTONOMY_HALT")):
                    return True
        except Exception:
            # H6 leg 3: fail CLOSED on a money path — an unprovable halt state
            # (the file check raised) is treated as HALTED, never silently "not
            # halted." This branch is unreachable in normal operation, so the
            # default no-halt envelope is byte-identical.
            return True
        return False

    # §4.3 (intelligence-stack finalization, 2026-07-09) — evidence-grounded
    # completion review for autonomous runs, DEFAULT ON: the claim (done() text)
    # is judged against the mechanical evidence pack (ledger/artifacts/refs).
    # 'unmet' (claim contradicted) -> record_failure; 'met' -> verified;
    # 'unclear'/error/timeout -> done (UNVERIFIED) — completes but is excluded
    # from the learning loops. Set =false to restore the unjudged legacy path.
    @staticmethod
    def goal_completion_judge() -> bool:
        return _bool_env("GOAL_COMPLETION_JUDGE", True)

    @staticmethod
    def goal_judge_timeout_sec() -> int:
        return _int_env("GOAL_JUDGE_TIMEOUT_SEC", 60)

    # §5.3: ancient blocked goals age out to 'cancelled' (visible, logged)
    # instead of rotting as permanent planner context. 0 disables aging.
    @staticmethod
    def goal_blocked_max_age_days() -> int:
        return _int_env("GOAL_BLOCKED_MAX_AGE_DAYS", 14)

    # Wake change-gate: a change-gated cron review
    # tick skips the paid model call when the tenant's observable state hasn't
    # moved since the last tick (cron/wake_gate.py). Posture `full` turns it on
    # by default — it pairs with CRON_ENABLED; per-job opt-in via
    # payload.change_gated, delivery jobs never gated.
    @staticmethod
    def wake_change_gate() -> bool:
        return _bool_env("WAKE_CHANGE_GATE",
                         _posture_autonomy_default("WAKE_CHANGE_GATE"))

    # §7.2 — blocker → owner escalation. When a goal trips the circuit breaker
    # (status='blocked') OR the pipeline drains, surface a concrete ask to the owner
    # instead of dying silently. Default OFF (an unsolicited owner push is opt-in).
    @staticmethod
    def goal_blocker_escalation() -> bool:
        # W1-1: default governed by AUTONOMY_POSTURE (owner-visible/full turn it on).
        return _bool_env("GOAL_BLOCKER_ESCALATION",
                         _posture_autonomy_default("GOAL_BLOCKER_ESCALATION"))

    @staticmethod
    def goal_empty_pipeline_escalate_after() -> int:
        """Consecutive planner runs that leave the ready queue EMPTY before the
        stall escalates to the owner (rides GOAL_BLOCKER_ESCALATION)."""
        return _int_env("GOAL_EMPTY_PIPELINE_ESCALATE_AFTER", 2)

    # §7.5 — autonomous continuity bridge. Carry a recent-activity summary INTO a
    # goal/cron tick (opposite scoping to the chat digest) so autonomous runs stop
    # re-deriving "nothing new" every tick. Default OFF (additive context; verify
    # token cost before flipping on).
    @staticmethod
    def autonomous_continuity_bridge() -> bool:
        # W1-1: default governed by AUTONOMY_POSTURE (owner-visible/full turn it on).
        return _bool_env("AUTONOMOUS_CONTINUITY_BRIDGE",
                         _posture_autonomy_default("AUTONOMOUS_CONTINUITY_BRIDGE"))

    # W5 — curator
    @staticmethod
    def curator_enabled() -> bool:
        return _bool_env("CURATOR_ENABLED", _safe_autonomy_default("CURATOR_ENABLED"))

    @staticmethod
    def curator_interval_hours() -> int:
        return _int_env("CURATOR_INTERVAL_HOURS", 168)

    @staticmethod
    def curator_stale_days() -> int:
        return _int_env("CURATOR_STALE_DAYS", 30)

    @staticmethod
    def curator_archive_days() -> int:
        return _int_env("CURATOR_ARCHIVE_DAYS", 90)

    # (curator_llm_merge / CURATOR_LLM_MERGE removed 2026-06-29 — the Phase-2 merge step
    #  it gated was a logged no-op with no merge policy. Re-add under its own flag when a
    #  concrete policy exists.)

    # C4 (2026-07-11) — mechanical note consolidation riding the curator tick:
    # archive agent-authored notes never read within the stale window + collapse
    # exact-duplicate notes. LLM-free by design (the aux-LLM clustering/
    # contradiction pass stays deferred until a concrete policy exists — the
    # CURATOR_LLM_MERGE lesson above). Archive-only, audited, never touches
    # owner-authored notes.
    @staticmethod
    def knowledge_curator_enabled() -> bool:
        return _bool_env("KNOWLEDGE_CURATOR_ENABLED",
                         _safe_autonomy_default("KNOWLEDGE_CURATOR_ENABLED"))

    @staticmethod
    def knowledge_note_stale_days() -> int:
        return _int_env("KNOWLEDGE_NOTE_STALE_DAYS", 90)

    # W6 — cross-session search tool (read-only, default-on)
    @staticmethod
    def memory_search_tool() -> bool:
        return _bool_env("MEMORY_SEARCH_TOOL", True)

    # W7 — insights tool (read-only authored-skill reuse metric)
    @staticmethod
    def insights_tool() -> bool:
        return _bool_env("INSIGHTS_TOOL", _safe_autonomy_default("INSIGHTS_TOOL"))

    # I-6 — agent_status introspection tool (read-only runtime self-report:
    # steps used/remaining, active tools, context usage, wallet+ledger)
    @staticmethod
    def agent_status_tool() -> bool:
        return _bool_env("AGENT_STATUS_TOOL", _safe_autonomy_default("AGENT_STATUS_TOOL"))

    # I-3 / H3 (dedup decision D1) — verify-before-done: bounded nudge (max 2
    # attempts) when the action ledger shows a code edit newer than the last
    # successful run_tests. See agents/task/runtime/edit_verify.py.
    @staticmethod
    def verify_before_done() -> bool:
        return _bool_env("VERIFY_BEFORE_DONE", _safe_autonomy_default("VERIFY_BEFORE_DONE"))

    # I-2 / H1 (dedup decision D2) — LSP diagnostics-after-edit: after a
    # successful str_replace/apply_patch/create_file, run an external type/lint
    # checker (pyright for .py, tsc for .ts/.tsx/.js/.jsx — see
    # tools/coding/lsp.py::diagnose_file) against the freshly-written file and
    # append an errors-only <diagnostics> block to the tool result. Wired
    # directly into tools/coding/tool.py (NOT a Controller transform hook).
    # Deterministic, no LLM call, fail-open (missing checker/timeout/parse
    # error => no-op). Default OFF and deliberately NOT in _SAFE_LOCAL_FLAGS
    # for v1 — spawns an external subprocess per successful edit; opt-in until
    # proven safe/fast enough to default on under POLYROB_LOCAL.
    @staticmethod
    def coding_lsp_enabled() -> bool:
        return _bool_env("CODING_LSP_ENABLED", False)

    # I-4 / H2 (dedup decision D3) — off-workspace shadow-git per-file
    # snapshot/restore: before a mutating coding action, commit the SINGLE
    # touched file into a shadow git repo living outside the workspace (see
    # tools/coding/snapshot.py), so a bad str_replace/apply_patch/delete is
    # recoverable via the `restore` action. Default OFF and deliberately NOT
    # in _SAFE_LOCAL_FLAGS for v1 — spawns git subprocesses per mutating edit;
    # opt-in until proven safe/fast enough to default on under POLYROB_LOCAL.
    @staticmethod
    def coding_snapshot_enabled() -> bool:
        return _bool_env("CODING_SNAPSHOT_ENABLED", False)

    # KB — knowledge-base feature gate (Task 2 / local_vector prerequisite)
    @staticmethod
    def kb_enabled() -> bool:
        return _bool_env("KB_ENABLED", _safe_autonomy_default("KB_ENABLED"))

    # C1 — context-reference expansion (@file/@folder/@diff/@url)
    # Default ON under POLYROB_LOCAL (single-user CLI), OFF on the server.
    @staticmethod
    def context_references_enabled() -> bool:
        return _bool_env(
            "CONTEXT_REFERENCES_ENABLED",
            _safe_autonomy_default("CONTEXT_REFERENCES_ENABLED"),
        )

    # C9 — auto-load CLAUDE.md/AGENTS.md/.cursorrules as a PROJECT_CONTEXT foundation
    # message. Default ON under POLYROB_LOCAL (single-user CLI), OFF on the server.
    @staticmethod
    def project_context_autoload() -> bool:
        return _bool_env(
            "PROJECT_CONTEXT_AUTOLOAD",
            _safe_autonomy_default("PROJECT_CONTEXT_AUTOLOAD"),
        )

    @staticmethod
    def project_context_max_tokens() -> int:
        return _int_env("PROJECT_CONTEXT_MAX_TOKENS", 20000)

    # Phase 2 — server-side project-context opt-in. When ON (and NOT local mode),
    # the loader runs on the server and the file is injected UNTRUSTED-WRAPPED
    # (framed as DATA, not instructions). Default OFF and deliberately NOT a
    # safe-local flag — POLYROB_LOCAL must not flip it on, so the multi-tenant
    # server stays byte-identical unless an operator explicitly opts in.
    @staticmethod
    def project_context_server_mode() -> bool:
        return _bool_env("PROJECT_CONTEXT_SERVER_MODE", False)

    # T13 — KB auto-prefetch (inject KB recall alongside memory recall at step start)
    # Default ON under POLYROB_LOCAL (single-user CLI), OFF on multi-tenant server.
    @staticmethod
    def kb_auto_prefetch() -> bool:
        return _bool_env("KB_AUTO_PREFETCH", _safe_autonomy_default("KB_AUTO_PREFETCH"))

    # Task 2 — episodic activity ledger (durable per-run provenance rows).
    # Default ON under POLYROB_LOCAL (single-user CLI) OR AUTONOMY_POSTURE
    # owner-visible/full (verified + owner-visible autonomy implies a durable
    # activity ledger, so episodic is part of the posture group).
    @staticmethod
    def episodic_memory_enabled() -> bool:
        return _bool_env("EPISODIC_MEMORY_ENABLED",
                         _safe_autonomy_default("EPISODIC_MEMORY_ENABLED")
                         or _posture_autonomy_default("EPISODIC_MEMORY_ENABLED"))

    # Task 3 — inject a recent-episodes digest into the session.
    @staticmethod
    def episodic_digest_inject() -> bool:
        return _bool_env("EPISODIC_DIGEST_INJECT",
                         _safe_autonomy_default("EPISODIC_DIGEST_INJECT")
                         or _posture_autonomy_default("EPISODIC_DIGEST_INJECT"))

    # Session-close reflection (consolidate a short session's findings
    # at close; one extra aux call per closed session). Posture-governed so an
    # owner-visible instance actually learns from its autonomous runs. Consumer:
    # modules/memory/task/task_context_manager.py (reads this resolver lazily).
    @staticmethod
    def reflection_on_session_close() -> bool:
        return _bool_env("REFLECTION_ON_SESSION_CLOSE",
                         _posture_autonomy_default("REFLECTION_ON_SESSION_CLOSE"))

    # Restart-durable autonomy state (background delegations + reentry
    # budgets in autonomy_state.db). Default ON; off restores volatile registries.
    @staticmethod
    def autonomy_state_durable() -> bool:
        return _bool_env("AUTONOMY_STATE_DURABLE", True)

    # Task 4 — cross-session continuity bridge (thread_key stitching).
    @staticmethod
    def continuity_bridge_enabled() -> bool:
        return _bool_env("CONTINUITY_BRIDGE_ENABLED", _safe_autonomy_default("CONTINUITY_BRIDGE_ENABLED"))

    # Task 4 — LLM-generated continuity summary at reset. Intentionally NOT in
    # _SAFE_LOCAL_FLAGS: OFF everywhere by default (adds latency at reset).
    @staticmethod
    def continuity_llm_summary() -> bool:
        return _bool_env("CONTINUITY_LLM_SUMMARY", False)  # OFF everywhere (latency at reset)

    # Task 2 — episodic row retention window (days); pruned on the curator tick.
    @staticmethod
    def episodic_retention_days() -> int:
        return _int_env("EPISODIC_RETENTION_DAYS", 90)

    # B3 (2026-07-11) — cross-session `memories` retention window (days), enforced
    # on the curator tick via provider.prune_memories. Only rows with a B2
    # provenance stamp are age-prunable (legacy stampless rows are exempt);
    # <=0 disables the sweep entirely. Deliberately generous default — recall
    # rows are cheap and the exact-dup collapse already bounds growth.
    @staticmethod
    def memory_retention_days() -> int:
        return _int_env("MEMORY_RETENTION_DAYS", 365)

    # T16 — interrupt-and-redirect: Ctrl-C mid-turn prompts for a redirect instruction
    # that becomes the next turn instead of silently aborting. Default OFF; NOT in
    # _SAFE_LOCAL_FLAGS (must be opt-in — changes SIGINT UX for all local users).
    @staticmethod
    def interrupt_redirect_enabled() -> bool:
        return _bool_env("INTERRUPT_REDIRECT", False)



def reset_autonomy_mode_warnings() -> None:
    """TEST-ONLY seam: clear the one-time full-autonomy clamp warning so the next
    ``full_autonomy_enabled()`` call re-evaluates (and may re-warn) from scratch.

    Replaces the previous ``monkeypatch.setattr(constants, "_FULL_AUTONOMY_WARNED", False)``
    poke, which no longer reaches this module's global after the WS-1 relocation. Production
    code never calls this.
    """
    global _FULL_AUTONOMY_WARNED
    _FULL_AUTONOMY_WARNED = False
