"""SendDecision: the base contract for 'is the agent allowed to send right now?'.

Default surfaces always ALLOW (free outbound). A windowed surface (WhatsApp's 24h
customer-service window) overrides Surface.can_send_now() to return TEMPLATE_ONLY or
DENY when outside the window, so proactive producers (cron/self-wake/correspondent
outreach) can suppress/queue/template instead of silently dropping a message.
"""
from enum import Enum


class SendDecision(str, Enum):
    ALLOW = "allow"                  # free-form send permitted
    TEMPLATE_ONLY = "template_only"  # only a pre-approved template may be sent
    DENY = "deny"                    # no message may be sent right now


def window_decision(window, last_inbound, now: float) -> SendDecision:
    """The ONE reading of a reply window (064 F4): ALLOW inside it, the window's
    ``outside`` decision after it or before any inbound. No window = ALLOW."""
    if window is None:
        return SendDecision.ALLOW
    if last_inbound is not None and (now - last_inbound) <= window.ttl_s:
        return SendDecision.ALLOW
    return SendDecision(window.outside)
