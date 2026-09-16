"""Client-facing activity classification (043 Work › Log).

The durable event log records dozens of ``kind`` strings (the SSOT is
``core/event_kinds.py``) plus a wider set of per-session *feed* kinds that
``webview/activity.py::summarize`` renders one line at a time. Neither carries a
grouping the owner can filter by — every kind was flat.

This module is that grouping, and NOTHING else. ``classify(kind)`` maps any
``kind`` string (durable OR feed OR unknown) to exactly one of
``CLIENT_CLASSES``; ``is_diagnostic(kind)`` names the low-signal engine-internal
kinds the Log hides until the owner asks for them ("show diagnostics").

Contracts:

- **Pure.** No I/O, no state, no side effects. Same input → same output.
- **Total.** Every string returns a member of ``CLIENT_CLASSES``; a genuinely
  unknown kind (and any non-string) is ``"system"``, never a crash. The Log
  must be able to render an event it has never seen before.
- **Core-tier.** Imports ``core.event_kinds`` and nothing else — the layering
  ratchet (``tests/test_layering_ratchet.py``) forbids ``core`` importing
  ``agents``/``tools``/``webview``/``surfaces``. The feed kinds
  ``webview/activity.py::summarize`` handles are matched by literal here (not by
  importing that module) precisely to keep this seam core-clean.

NO ``from __future__ import annotations`` is needed (plain ``str`` hints), but it
is also harmless here — this module registers no introspected action closures.
"""

from core import event_kinds as ek

#: The ordered set of owner-facing classes. The Log renders one filter chip per
#: entry, in this order; ``classify`` never returns anything outside it.
CLIENT_CLASSES = ("goal", "cron", "money", "message", "system", "tool")

# --- money ---------------------------------------------------------------------
# Wallet spend, every payment/invoice leg, subscriptions, paid-room actions and
# on-chain execution. The open-ended families (payment_/subscription_/
# room_action_/tx_) are ALSO matched by prefix below so a future leg is money
# without a code change here; the explicit set documents today's members and
# guarantees every current durable kind is covered.
_MONEY_KINDS = frozenset({
    ek.WALLET_SPEND,
    ek.PAYMENT_REQUESTED,
    ek.PAYMENT_SETTLED,
    ek.PAYMENT_EXPIRED,
    ek.PAYMENT_UNMATCHED,
    ek.PAYMENT_SELF_PROCEEDS,
    ek.PAYMENT_SETTLING_REVERTED,
    ek.PAYMENT_FEEDBACK_AUTHORIZED,
    ek.PAYMENT_AUTO_APPROVED,
    ek.SUBSCRIPTION_CREATED,
    ek.SUBSCRIPTION_RENEWED,
    ek.SUBSCRIPTION_RENEWAL_INVOICED,
    ek.SUBSCRIPTION_GRACE,
    ek.SUBSCRIPTION_SUSPENDED,
    ek.SUBSCRIPTION_CANCELED,
    ek.SUBSCRIPTION_APPLY_FAILED,
    ek.ROOM_ACTION_OFFERED,
    ek.ROOM_ACTION_SETTLED,
    ek.ROOM_ACTION_APPLIED,
    ek.ROOM_ACTION_CREDITED,
    ek.ROOM_ACTION_CREDIT_REDEEMED,
    ek.TX_BROADCAST,
    ek.TX_SETTLED,
})
_MONEY_PREFIXES = ("payment_", "subscription_", "room_action_", "tx_")

# --- goal ----------------------------------------------------------------------
# The goal board: durable run/completion rows plus every ``goal_*`` board verb
# (created/claimed/succeeded/failed/blocked/gave_up …) that only ever appears as
# a prefixed kind, exactly as ``activity.py::summarize`` matches it.
_GOAL_KINDS = frozenset({
    ek.GOAL_RUN,
    ek.GOAL_COMPLETION,
})
_GOAL_PREFIXES = ("goal_",)

# --- message -------------------------------------------------------------------
# Communication the owner cares about: deliveries, owner notices, outbound
# sends, public posts, correspondent bindings and the inbound that became a turn.
# (An inbound that was TURNED AWAY is a perimeter/security note → system.)
_MESSAGE_KINDS = frozenset({
    ek.OWNER_NOTICE,
    ek.USER_DELIVERY,
    ek.OUTBOUND_OPEN_SEND,
    ek.SOCIAL_WRITE,
    ek.CORRESPONDENT_PENDING,
    ek.CORRESPONDENT_RESUMED,
    ek.INBOUND_ROUTED,
})

# --- tool ----------------------------------------------------------------------
# The agent using tools: the typed result (A16), the legacy execution/started
# feed kinds, tool-governance outcomes and the actions-registered notice. Every
# ``tool_*`` feed/durable kind is also caught by prefix. Installing a skill or
# an MCP server is a setup/self-modification event, NOT tool use → system.
_TOOL_KINDS = frozenset({
    "tool_result",       # A16 typed feed event (WS-E), not in the durable SSOT
    "tool_execution",    # legacy feed kind
    "available_actions",
    ek.TOOL_DENIED,
    ek.TOOL_TIMEOUT,
    ek.TOOL_AUTO_APPROVED,
})
_TOOL_PREFIXES = ("tool_",)

# --- cron ----------------------------------------------------------------------
_CRON_KINDS = frozenset({ek.CRON_RUN})

# --- diagnostics ---------------------------------------------------------------
# Low-signal engine-internal kinds the Log hides until "show diagnostics". These
# still classify (they are ``system``/``tool``); ``is_diagnostic`` is an
# orthogonal visibility flag, not a class.
_DIAGNOSTIC_KINDS = frozenset({
    "llm_started",
    "llm_request",
    "retry_wait",
    "step",
    "tool_started",
})
_DIAGNOSTIC_PREFIXES = ("compaction_",)  # compaction_started / compaction_finished


def classify(kind):
    # type: (str) -> str
    """Map a durable/feed/unknown ``kind`` to one member of ``CLIENT_CLASSES``.

    Total and pure: an empty, unknown, or non-string kind is ``"system"``.
    Precedence is money → cron → goal → tool → message → system, so a family
    prefix never steals a kind another class owns (no kind starts with another
    class's prefix).
    """
    if not isinstance(kind, str) or not kind:
        return "system"
    if kind in _MONEY_KINDS or kind.startswith(_MONEY_PREFIXES):
        return "money"
    if kind in _CRON_KINDS:
        return "cron"
    if kind in _GOAL_KINDS or kind.startswith(_GOAL_PREFIXES):
        return "goal"
    if kind in _TOOL_KINDS or kind.startswith(_TOOL_PREFIXES):
        return "tool"
    if kind in _MESSAGE_KINDS:
        return "message"
    return "system"


def is_diagnostic(kind):
    # type: (str) -> bool
    """True for the low-signal engine kinds the Log hides by default.

    Orthogonal to ``classify`` — a diagnostic kind still has a class; this only
    governs default visibility ("show diagnostics"). Pure and total.
    """
    if not isinstance(kind, str) or not kind:
        return False
    return kind in _DIAGNOSTIC_KINDS or kind.startswith(_DIAGNOSTIC_PREFIXES)
