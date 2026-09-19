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
#: 043 A8/A42: the durable record of which loops `start_autonomy` actually
#: started this process — the status snapshot's `expected` liveness set reads
#: the newest row's `attrs.loops` so a loop the runtime never started (a
#: disabled gate) is never mistaken for a silently-dead one.
AUTONOMY_STARTED = "autonomy_started"
CRON_RUN = "cron_run"
#: 056 WS4/WS9: an SMTP login was rejected (535) — the layering-safe fact the
#: status snapshot turns into a health WARN with the remedy.
EMAIL_AUTH_REJECTED = "email_auth_rejected"
#: 056 WS9: an external-rail call outcome (anysite …): ok / empty / error / timeout.
RAIL_PROBE = "rail_probe"
#: 056 WS8: one stale-session-directory GC pass (dry-run or applied) — the
#: candidate count / bytes the owner reads before flipping SESSION_DIR_GC_APPLY.
SESSION_GC = "session_gc"
#: 043 A29: the SERVICE-level cron domain events — a job was scheduled or
#: cancelled through ``cron.service.CronService`` (the SSOT every surface calls).
#: Distinct from the console-action audit ``CONSOLE_CRON_CANCEL`` below: these
#: name the domain change (and its actor + ``via`` surface) no matter which seat
#: — console, CLI or Telegram — drove it, so the audit is complete even for the
#: seats ``console_write`` never saw.
CRON_SCHEDULED = "cron_scheduled"
CRON_CANCELLED = "cron_cancelled"
GOAL_RUN = "goal_run"
GOAL_COMPLETION = "goal_completion"          # consumed by cron/digest rollup
SELF_WAKE = "self_wake"
SELF_MODIFICATION = "self_modification"
DELEGATION_INTERRUPTED = "delegation_interrupted"
DELEGATION_DELIVERED = "delegation_delivered"   # T1.6: completed-undelivered drain stamp
RUN_OUTCOME_DEGRADED = "run_outcome_degraded"
CREDIT_SENTINEL = "credit_sentinel"
AUTONOMY_PAUSED = "autonomy_paused"       # 031: owner/system pause record written
AUTONOMY_RESUMED = "autonomy_resumed"     # 031: record cleared (owner, or expiry)
PAUSE_VIOLATION = "pause_violation"       # 031: autonomous activity recorded after a pause

# --- outbound content (public/irreversible surfaces) -----------------------------
SOCIAL_WRITE = "social_write"       # 2026-08-28: durable cross-session cooldown for
                                     # autonomous twitter_post/twitter_thread — the
                                     # in-memory per-instance rate limiter never saw
                                     # a repeat goal firing in a FRESH session

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

# --- perimeter (045 lane 1) -----------------------------------------------------
#: One row per inbound routing decision, allowed and denied alike. The ONLY
#: place the question "who talked to the agent, and who was turned away" is
#: answerable from. Bodies are NEVER stored — length and an 8-char hash only.
INBOUND_ROUTED = "inbound_routed"
#: An inbound that never became a turn: an allowlist drop, a tier denial, a
#: missing mention, a participant with no bound session. Carries `reason`.
ACCESS_DENIED = "access_denied"
#: A threat scan (`modules.memory.task.threat_scan.is_suspicious`) flagged
#: content. The local verdict is unchanged; this only makes it visible.
INJECTION_FLAGGED = "injection_flagged"

# --- money / wallet -------------------------------------------------------------
WALLET_SPEND = "wallet_spend"
PAYMENT_REQUESTED = "payment_requested"
PAYMENT_SETTLED = "payment_settled"
PAYMENT_EXPIRED = "payment_expired"
PAYMENT_UNMATCHED = "payment_unmatched"
#: An inbound treasury transfer confirmed as our OWN trade proceeds (an exact
#: broadcast-hash match against the wallet audit ledger) rather than a payment.
#: Not an owner notice — but never a silent skip either.
PAYMENT_SELF_PROCEEDS = "payment_self_proceeds"
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

# --- paid room actions (046) ------------------------------------------------------
#: A paid moderation offer was MINTED. Not money yet — an invitation to pay.
ROOM_ACTION_OFFERED = "room_action_offered"
#: Its invoice settled. The money has arrived; the effect has not run yet.
ROOM_ACTION_SETTLED = "room_action_settled"
#: The effect landed in the room.
ROOM_ACTION_APPLIED = "room_action_applied"
# ⚠️ There is deliberately NO `room_action_failed`. It shipped as a constant
# "reserved for a terminal non-credit failure" that nothing produced, and this
# module's own contract test is the rule against exactly that: every apply
# failure pays a CREDIT and emits `room_action_credited`, so a second kind for
# the same event would only ever be a category nobody could fill.
#: ⚠️ Money we HOLD against an undelivered service. The status snapshot leads
#: with these, and the owner is notified.
ROOM_ACTION_CREDITED = "room_action_credited"
#: A held credit was SPENT on a later free offer (046 phase 2). Before it
#: existed, `redeemable_credit` had zero callers and the credit was a promise
#: nothing in the tree could keep.
ROOM_ACTION_CREDIT_REDEEMED = "room_action_credit_redeemed"

# --- durable app service (032) ----------------------------------------------------
APP_REQUESTED = "app_requested"      # a NEW slug wrote a pending row (the owner ask)
APP_APPROVED = "app_approved"        # owner approved the address / a redeploy was queued
APP_LIVE = "app_live"                # supervisor: healthy behind its URL
APP_FAILED = "app_failed"            # supervisor: docker/nginx/health failure
APP_STOPPED = "app_stopped"          # owner kill / agent app_stop / pause edge
# On-chain money execution (039 Unit A). Two kinds, not one: the window between
# a broadcast and its settlement is exactly when a transaction is invisible, and
# a single "it happened" event cannot describe it.
TX_BROADCAST = "tx_broadcast"
TX_SETTLED = "tx_settled"

#: An inbound TRANSPORT fault on a polling surface (telegram long-poll, IMAP)
#: — the poller recovers, which is exactly why nothing surfaced it. Prod logged
#: 76 `get_updates failed` in 7 days (timeouts, connection resets, Bad Gateway)
#: and no seat could report that the agent had been intermittently unreachable.
#: Recovery is not the same as health. Rate-limited at the producer.
SURFACE_POLL_ERROR = "surface_poll_error"

# --- console writes (043 W4) ----------------------------------------------------
#: The owner acted through the web console. One kind per mutating-route class,
#: each carrying ``via="webview"`` + the verb detail in ``attrs`` (see
#: ``webview/audit.py::console_write``), so the durable audit can name WHO
#: changed state and from WHERE — the actor trail the console lacked while only
#: the pause/resume and app verbs recorded downstream.
CONSOLE_CONFIG_WRITE = "console_config_write"
CONSOLE_PREF_WRITE = "console_pref_write"
CONSOLE_PENDING_DECIDE = "console_pending_decide"
CONSOLE_GOAL_VERB = "console_goal_verb"
CONSOLE_GOAL_CREATE = "console_goal_create"
CONSOLE_CRON_CANCEL = "console_cron_cancel"
CONSOLE_CRON_CREATE = "console_cron_create"
CONSOLE_INVOICE_SETTLE = "console_invoice_settle"
CONSOLE_PFP_WRITE = "console_pfp_write"
CONSOLE_INBOX_DECIDE = "console_inbox_decide"
CONSOLE_SELF_CONTEXT_WRITE = "console_self_context_write"
CONSOLE_MEMORY_WRITE = "console_memory_write"

# Infra/storage housekeeping
DB_RELOCATED = "db_relocated"                 # R-2 T3 one-shot sidecar move ran

KNOWN_KINDS = frozenset(
    v for k, v in globals().items() if k.isupper() and isinstance(v, str)
)
