"""Static LLM-outage notice for owner-facing chat-surface turns (proposal 015 #2).

2026-07-16 prod incident: the owner asked "what did you spend money on?" over
Telegram, OpenRouter 402'd, retries exhausted ("ALL LLM PROVIDERS EXHAUSTED"),
session 4883c075 ended failed — and ZERO outbound sends happened. The owner got
nothing: no reply, no error. This module supplies the LLM-independent pieces of
the fix: the kill-switch flag, the outage classifier (reusing the credit-death
SSOT ``core.credit_sentinel.looks_like_credit_death``), the static notice text,
and a per-(surface+chat) cooldown so a 402 storm sends at most ONE notice per
window.

Send-site: ``surfaces/telegram/harness.py::_run_and_deliver`` — the ONE shared
post-run delivery seam every chat surface (telegram/slack/discord/signal/x/
email) routes through via ``act_on_inbound``. Goal/cron/self-wake/background
runs call ``run_session`` directly and never pass through that seam, so the
notice structurally cannot fire for them.

Flag: ``LLM_OUTAGE_NOTICE`` (default **ON** — deliberate: an outage notice that
defaults OFF would never fire; precedent ``UNTRUSTED_TOOL_RESULT_WRAP``).

Cooldown choice: a module-level in-process timestamp map, NOT a durable store.
The notice path runs inside an error handler and must stay dependency-free and
fail-open — a DB/file write is one more thing that can raise exactly when the
system is already broken. Worst case after a process restart is ONE extra
notice, the safe direction for an owner-facing outage signal.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from core.credit_sentinel import looks_like_credit_death
from core.env import bool_env

logger = logging.getLogger(__name__)

#: At most one notice per (surface+chat) key per this window.
LLM_OUTAGE_COOLDOWN_SEC = 30 * 60

#: Static, LLM-independent text. The ⚠️ prefix + "automated notice" framing make
#: it clearly NOT an agent reply (proposal 015 risk note: must be
#: distinguishable from genuine agent output).
OUTAGE_NOTICE_TEXT = (
    "⚠️ Rob hit an infrastructure error (LLM provider unavailable or out of "
    "credits) and couldn't process your last message. This is an automated "
    "notice, not a reply — your message was logged and the owner has been "
    "notified."
)

# Exhaustion strings the credit-death SSOT does NOT match. Sources (verified):
# - "All LLM providers exhausted: [...]" — execute_session's
#   LLMProviderExhaustedError catch (agents/task/session/execution.py) → the
#   run_session status string "Session failed: All LLM providers exhausted: …";
# - "All LLM providers failed. Tried: [...]" — the terminal ActionResult from
#   error_recovery._handle_step_error's provider-exhausted branch;
# - "No fallback available after …" — llm_runner's raised
#   LLMProviderExhaustedError message;
# - "Permanent LLM error: …" — execute_session's LLMPermanentError catch;
# - "PERMANENT ERROR: …" — the terminal ActionResult from the is_permanent halt
#   branch (only ever minted for LLM-permanent/auth/billing errors).
_EXHAUSTION_MARKERS = (
    "all llm providers",
    "providers exhausted",
    "no fallback available",
    "permanent llm error",
    "permanent error",
    "llmpermanenterror",
    "llmproviderexhaustederror",
)

# key -> monotonic-ish wall-clock timestamp of the last notice sent.
_last_notice_at: Dict[str, float] = {}


def llm_outage_notice_enabled() -> bool:
    """Kill-switch for the static LLM-outage notice (default ON)."""
    return bool_env("LLM_OUTAGE_NOTICE", True)


def looks_like_llm_outage(*texts: Optional[str]) -> bool:
    """Does any of these framework strings look like total LLM-provider failure?

    Called on run_session status strings ("Session failed: …") and terminal
    ActionResult errors — never on agent prose. Reuses the credit-death SSOT
    (402/insufficient_quota/billing/…) and adds the provider-exhaustion shapes
    it doesn't cover (see ``_EXHAUSTION_MARKERS``).
    """
    for text in texts:
        if not text:
            continue
        low = str(text).lower()
        if looks_like_credit_death(low):
            return True
        if any(m in low for m in _EXHAUSTION_MARKERS):
            return True
    return False


def should_send_llm_outage_notice(key: str, now: Optional[float] = None) -> bool:
    """Flag + cooldown gate: True == the caller should send ONE notice now.

    A True return MARKS the window for ``key`` (check-and-set), so callers must
    only ask when they are about to send. ``key`` should be surface+chat-scoped
    (the chat binding ``session_key``); a falsy key degrades to one global
    bucket rather than bypassing the cooldown. Never raises (fail-open into
    "don't send" — the safe direction inside an error path).
    """
    try:
        if not llm_outage_notice_enabled():
            return False
        bucket = str(key or "") or "_global"
        ts = time.time() if now is None else float(now)
        last = _last_notice_at.get(bucket)
        if last is not None and (ts - last) < LLM_OUTAGE_COOLDOWN_SEC:
            return False
        _last_notice_at[bucket] = ts
        return True
    except Exception:
        logger.debug("llm outage notice gate failed (fail-open: no send)",
                     exc_info=True)
        return False


def reset_llm_outage_notice_state() -> None:
    """Test seam: clear the in-process cooldown map."""
    _last_notice_at.clear()
