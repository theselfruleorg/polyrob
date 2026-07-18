"""SSOT for durable event-log ``kind`` strings (audit T9, 2026-07-16).

Producers (``TelemetryEventLog.record(kind, ...)`` — directly or via the
``modules/x402`` ``_emit`` / controller ``_emit_governance_event`` wrappers) and
consumers (webview activity feed, spend rollups, digest aggregation) previously
each hard-coded the same free-typed strings; a rename/typo on either side
silently dropped events from the owner-facing views with no error.

Add new kinds HERE and import the constant at both ends — the contract test
(tests/unit/core/test_event_kinds.py) greps every producer call site into
agreement. Lives in ``core/`` (not ``agents/task/telemetry/``) so ``core`` and
``webview`` consumers never need an upward import into ``agents``.
"""

# --- agent / autonomy lifecycle -------------------------------------------------
AUTONOMY_TICK = "autonomy_tick"
CRON_RUN = "cron_run"
GOAL_RUN = "goal_run"
GOAL_COMPLETION = "goal_completion"          # consumed by cron/digest rollup
SELF_WAKE = "self_wake"
SELF_MODIFICATION = "self_modification"
DELEGATION_INTERRUPTED = "delegation_interrupted"
RUN_OUTCOME_DEGRADED = "run_outcome_degraded"
CREDIT_SENTINEL = "credit_sentinel"

# --- delivery / correspondence --------------------------------------------------
USER_DELIVERY = "user_delivery"
OWNER_NOTICE = "owner_notice"
OUTBOUND_OPEN_SEND = "outbound_open_send"
CORRESPONDENT_PENDING = "correspondent_pending"
CORRESPONDENT_RESUMED = "correspondent_resumed"

# --- tool governance ------------------------------------------------------------
TOOL_DENIED = "tool_denied"
TOOL_TIMEOUT = "tool_timeout"
TOOL_AUTO_APPROVED = "tool_auto_approved"
PAYMENT_AUTO_APPROVED = "payment_auto_approved"
MCP_INSTALL = "mcp_install"

# --- money / wallet -------------------------------------------------------------
WALLET_SPEND = "wallet_spend"
PAYMENT_REQUESTED = "payment_requested"
PAYMENT_SETTLED = "payment_settled"
PAYMENT_EXPIRED = "payment_expired"
PAYMENT_UNMATCHED = "payment_unmatched"
PAYMENT_SETTLING_REVERTED = "payment_settling_reverted"
PAYMENT_FEEDBACK_AUTHORIZED = "payment_feedback_authorized"

# --- subscriptions ---------------------------------------------------------------
SUBSCRIPTION_CREATED = "subscription_created"
SUBSCRIPTION_RENEWED = "subscription_renewed"
SUBSCRIPTION_RENEWAL_INVOICED = "subscription_renewal_invoiced"
SUBSCRIPTION_GRACE = "subscription_grace"
SUBSCRIPTION_SUSPENDED = "subscription_suspended"
SUBSCRIPTION_CANCELED = "subscription_canceled"
SUBSCRIPTION_APPLY_FAILED = "subscription_apply_failed"

# Infra/storage housekeeping
DB_RELOCATED = "db_relocated"                 # R-2 T3 one-shot sidecar move ran

KNOWN_KINDS = frozenset(
    v for k, v in globals().items() if k.isupper() and isinstance(v, str)
)
