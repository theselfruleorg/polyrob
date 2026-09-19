"""Per-feature capability toggles and runtime knobs (message tool, tool rig, prefs, invoice card,
EIP-8004 feedback, persona block, memory cadence, tickers, compaction guard, dead-target registry,
DeFi tiers, memory backend).

Split out of ``core/config_policy/policy.py`` (S2, 2026-08-29); ``policy.py`` re-exports
every name so existing importers are unaffected. Import order (a DAG): _env -> local_profile
-> autonomy_mode -> autonomy_posture -> compute_posture -> payment_policy -> capability_toggles
-> autonomy_config -> runtime_gates.
"""

import os
from core.config_policy._env import _bool_env, _float_env, _int_env
from core.config_policy.local_profile import _safe_autonomy_default, local_mode_enabled
from core.config_policy.autonomy_mode import _mode_capability_default


def run_budget_usd() -> float:
    """RUN_BUDGET_USD — session-cumulative provider-spend ceiling in USD (T1.1).

    0 (default) = disabled. When > 0, the run loop halts honestly before the
    next step once the session's summed usage_records api_cost_usd reaches the
    ceiling (agents/task/agent/core/run_budget.py). Live-read at access time so
    tests can monkeypatch. Distinct from the RETIRED AUTONOMY_BUDGET_USD rate
    ceiling (see tests/unit/agents/task/runtime/test_budget_gate_removed.py) —
    this gates finite spend, not a $/day rate.
    """
    return _float_env("RUN_BUDGET_USD", 0.0)


def code_exec_enabled() -> bool:
    """`CODE_EXEC_ENABLED` (default OFF). Core-tier home (056 WS4) so the agents
    tier can read it without an upward import; `tools.code_exec` delegates here."""
    return _bool_env("CODE_EXEC_ENABLED", False)


def shell_tools_enabled() -> bool:
    """`SHELL_TOOLS_ENABLED` — defaults ON at AGENT_COMPUTE_POSTURE>=1, an explicit
    env always wins. Core-tier home (056 WS4); `tools.shell` delegates here."""
    from core.config_policy._env import posture_flag
    return posture_flag("SHELL_TOOLS_ENABLED", 1)


def coding_tools_enabled() -> bool:
    """`CODING_TOOLS_ENABLED` — explicit env wins, else ON under POLYROB_LOCAL
    (the safe group). Core-tier home (056 WS4); `tools.coding` delegates here."""
    from core.config_policy._env import safe_local_flag
    return safe_local_flag("CODING_TOOLS_ENABLED")


def email_provider(env=None) -> str:
    """Which transport backs the agent's email: ``smtp`` | ``agentmail``.

    An explicit ``EMAIL_PROVIDER`` always wins; any other value (incl. the
    ``auto`` default) resolves to ``agentmail`` iff ``AGENTMAIL_API_KEY`` is set
    — the one-env-var "agent has its own inbox by default" path — else ``smtp``
    (legacy GMAIL_* IMAP/SMTP, byte-identical). Unknown values fall back to
    ``smtp`` so a typo can never route mail to an unintended provider.
    """
    src = os.environ if env is None else env
    raw = (src.get("EMAIL_PROVIDER") or "auto").strip().lower()
    if raw in ("smtp", "agentmail"):
        return raw
    return "agentmail" if (src.get("AGENTMAIL_API_KEY") or "").strip() else "smtp"


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


def owner_message_cooldown_seconds() -> int:
    """Minimum gap an autonomous/forged turn must respect before proactively
    messaging the OWNER again. Default 2h (7200s); `<=0` disables the guard.
    A genuine owner-initiated interactive turn is NEVER gated by this — only
    forged/autonomous sends (goal/cron/planner sessions, sub-agents, self-wake
    re-entries) are checked, via the durable conversation store's real send
    history (never the model's own self-report of elapsed time).

    2026-08-27 dedup-guard finding: a fresh goal session has no visibility
    into a SIBLING session's send minutes/hours earlier, so each retry of a
    failed 'deliver the owner ask'-style goal re-sent the identical content —
    4 genuine sends of the same consolidated ask landed in the owner's
    Telegram within ~4 hours, each with the goal's own narrative FALSELY
    claiming '24h cadence respected'. This closes the gap with an
    authoritative, code-level check instead of trusting that self-report.
    """
    return _int_env("OWNER_MESSAGE_COOLDOWN_SEC", 7200)


def tool_progressive_disclosure() -> bool:
    """Whether the dynamic tool rig is on: the honest ``<tool-catalog>`` foundation
    block (S1) + the ``load_tool`` self-serve action (S2, container-servable tools
    only). Default OFF; ON under POLYROB_LOCAL via the _SAFE_LOCAL_FLAGS group. An
    explicit TOOL_PROGRESSIVE_DISCLOSURE always wins.

    Hard lines regardless of this flag: money tools are never loadable via
    load_tool (explicit owner/goal grant only), delegate-blocked ids stay blocked
    for leaves, and correspondent-taint/posture/approval gates apply unchanged —
    loading only registers schemas (tools/tool_disclosure.py).
    """
    return _bool_env("TOOL_PROGRESSIVE_DISCLOSURE",
                      _safe_autonomy_default("TOOL_PROGRESSIVE_DISCLOSURE"))


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

    ⚠️ 046 phase 2 ORs in "this instance invoices at all". The local-group
    default alone meant a SERVER — the only posture that actually bills a
    stranger — never rendered a card, so the payer got an address to retype by
    hand and no QR at all. An instance with `X402_INVOICE_ENABLED` on has
    already decided to ask people for money; making the ask legible is not a
    separate decision. An explicit `INVOICE_CARD_ENABLED` still wins.

    ⚠️ The x402 enablement is recomputed HERE rather than imported from
    `modules.x402.invoicing` (its SSOT): `core` may not import `modules`, and
    the layering ratchet reads imports statically. `core/autonomy_runtime.py`
    duplicates the same guarded-OR for the same reason.
    """
    try:
        x402_on = _bool_env("X402_INVOICE_ENABLED",
                            _mode_capability_default("X402_INVOICE_ENABLED"))
    except Exception:
        x402_on = False
    return _bool_env("INVOICE_CARD_ENABLED",
                     _safe_autonomy_default("INVOICE_CARD_ENABLED") or x402_on)


def room_action_card_enabled() -> bool:
    """Whether a 046 paid-room-action OFFER also carries a payment card (QR).

    Defaults to :func:`room_actions_enabled` — a room that sells actions wants
    its offers legible, and the payer is a stranger in a chat window.

    ⚠️ Deliberately NOT `INVOICE_CARD_ENABLED`, which defaults to
    `local_mode_enabled()` and is therefore OFF on every server: reusing it
    would have meant that no deployment that can actually host a room ever
    rendered a QR. Render failures are fail-open at the call site
    (`surfaces/telegram/group_ops.py::_offer_card`) — a card is a picture of
    facts the text already carries.
    """
    from core.surfaces.room_actions import room_actions_enabled
    return _bool_env("ROOM_ACTION_CARD_ENABLED", room_actions_enabled())


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


def compaction_prompt_guard() -> bool:
    """Whether the compaction summarizer prompt + rebuilt summary carry explicit
    anti-injection framing (T1.3, parity with the reference compaction guard).

    A hostile mid-history payload (e.g. a tool result or user turn containing
    "ignore prior instructions and...") sits in the raw middle that gets fed
    verbatim to the (often smaller, cheaper) compaction aux model. Without
    framing, that payload can steer the summarizer itself, or the resulting
    summary can later be read by the MAIN model as live instructions rather
    than derived reference context.

    Resolved at access time. Default ON: on, ``_build_compaction_prompt``
    prepends a SECURITY preamble and wraps the conversation body in
    ``<conversation_data>``/``</conversation_data>`` literals, and
    ``_rebuild_with_summary`` appends a one-line reminder inside the existing
    compacted-history markers. OFF reproduces the exact legacy prompt/rebuild
    bytes (byte-identical, test-locked) — set ``COMPACTION_PROMPT_GUARD=false``
    to restore it.
    """
    return _bool_env("COMPACTION_PROMPT_GUARD", True)


def reflection_llm_enabled_default() -> bool:
    """Whether H-MEM phase reflection synthesizes summaries via the aux LLM (UP-09).

    Default **ON** (mirrors the MEMORY_BACKEND default-on precedent). Disable with
    REFLECTION_LLM_ENABLED in {none, off, false, 0, no, ''}.

    SINGLE SOURCE OF TRUTH: both the model-provisioning site (construction.py) and the
    runtime guard (TaskContextManager.__init__) MUST read this helper. The historical bug
    (Fusion-validated 2026-06-16) was that the runtime guard read
    `BotConfig.get("REFLECTION_LLM_ENABLED", False)` — and `BotConfig.get` is
    `getattr(self, key, default)` with no such attribute, so it was ALWAYS False — while
    construction.py read os.getenv. The two sources disagreed and reflection never fired.
    Relocated from agents/task/constants.py (S5, 2026-08-29) — a flag accessor belongs in
    the config plane, and modules/memory imported it upward. Semantics kept exactly: an
    explicit EMPTY value disables (``parse_bool``), unlike ``bool_env``'s blank-is-default.
    """
    raw = os.getenv("REFLECTION_LLM_ENABLED")
    if raw is None:
        return True
    from core.env import parse_bool
    return parse_bool(raw, True)


def dead_target_registry_enabled() -> bool:
    """Whether outbound sends are gated against a persisted dead-target registry
    (T1.5, catch-up Tier-1 item) so a provably-dead target (bot blocked,
    chat/user deleted) is skipped instead of retried forever.

    Task 1 ships the store (``core/surfaces/dead_targets.py::DeadTargetStore``)
    and the ``classify_dead_error`` liveness classifier only; this accessor is
    consumed by the outbound choke points added in later tasks. Live-read at
    access time. Default ON: set ``DEAD_TARGET_REGISTRY=false`` to disable the
    gate (byte-identical legacy retry-forever behavior at every wired call site).
    """
    return _bool_env("DEAD_TARGET_REGISTRY", True)


# MEMORY_BACKEND default SSOT: memory_policy.py (extracted; re-exported here).
from core.config_policy.memory_policy import (  # noqa: F401,E402
    memory_backend_default,
    resolved_memory_backend,
)


def defi_data_enabled() -> bool:
    """``DEFI_DATA_ENABLED`` — read-only DeFi sight (proposal 023 T0+T1).

    Tier-0 SSOT so both consumers (``tools/defi`` registration,
    ``agents/task/tool_defaults`` session tool_ids) import DOWNWARD and cannot
    disagree. Default OFF and deliberately NOT in ``_SAFE_LOCAL_FLAGS`` — prod
    runs ``POLYROB_LOCAL=1`` beside a live mainnet wallet.
    """
    from core.env import bool_env
    return bool_env("DEFI_DATA_ENABLED", False)


def defi_trade_enabled() -> bool:
    """``DEFI_TRADE_ENABLED`` — on-chain money verbs (proposal 023 T3).

    Default OFF and never in ``_SAFE_LOCAL_FLAGS``. Same tier-0 SSOT reasoning
    as :func:`defi_data_enabled`, with more at stake: this one broadcasts.
    """
    from core.env import bool_env
    return bool_env("DEFI_TRADE_ENABLED", False)
