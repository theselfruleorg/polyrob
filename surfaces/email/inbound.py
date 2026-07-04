"""WS-B email inbound pipeline — dedup -> identify -> route (transport-free).

The IMAP harness parses raw messages into a normalized dict and hands it here; this
module never touches the network, so the whole inbound spine is unit-testable without a
mailbox. Ordering mirrors the Telegram surface: dedup FIRST (by Message-ID), then
identify, then route.

Normalized message dict shape (produced by the harness):
    {
      "message_id":  "<id@host>",      # RFC 5322 Message-ID (dedup + idempotency key)
      "from":        "Name <a@b.com>",  # raw From header
      "subject":     "Re: ...",
      "body":        "...",             # plain-text body (quoted history is truncated here)
      "in_reply_to": "<out@rob>",       # In-Reply-To header (the thread anchor)
      "references":  "<root> <out>",    # References header (fallback thread anchor)
    }
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any, Optional

from core.surfaces.dispatcher import RouteDecision, route_inbound
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource

logger = logging.getLogger(__name__)

# Lines that introduce quoted history (cut everything from the first match). Covers the
# common clients + a few localized attributions + the Outlook "Original Message" divider.
_QUOTE_BOUNDARY_RES = [
    re.compile(r"^\s*On .*wrote:\s*$", re.IGNORECASE),          # Gmail / Apple (English)
    re.compile(r"^\s*Le .*écrit\s*:\s*$", re.IGNORECASE),       # French
    re.compile(r"^\s*Am .*schrieb:\s*$", re.IGNORECASE),        # German
    re.compile(r"^\s*El .*escribió:\s*$", re.IGNORECASE),       # Spanish
    re.compile(r"^\s*-{3,}\s*Original Message\s*-{3,}\s*$", re.IGNORECASE),  # Outlook
    re.compile(r"^\s*_{5,}\s*$"),                               # Outlook divider rule
]


@dataclass
class InboundResult:
    inbound: InboundMessage
    decision: RouteDecision


def parse_from_address(raw_from: str) -> str:
    """Extract the bare, normalized email address from a From header. '' if none."""
    _, addr = parseaddr(raw_from or "")
    return (addr or "").strip().lower()


def truncate_quoted_history(body: str) -> str:
    """Drop quoted reply history from a plain-text body (anti-smuggling).

    Cuts at the first quote-boundary line (an attribution like "On ... wrote:", a
    localized variant, or the Outlook "-----Original Message-----"/divider), so a
    correspondent reply can't smuggle a forged "owner said: ..." block below it. If no
    boundary is found, strips only a TRAILING contiguous block of ``>``-quoted lines —
    it does NOT delete a leading/interior ``>`` line the correspondent actually wrote
    (that was a data-loss bug). Best-effort + fail-soft.
    """
    try:
        lines = (body or "").splitlines()
        cut = len(lines)
        for i, line in enumerate(lines):
            if any(rx.match(line) for rx in _QUOTE_BOUNDARY_RES):
                cut = i
                break
        kept = lines[:cut]
        # If no explicit boundary matched, drop only a trailing run of quoted lines.
        if cut == len(lines):
            while kept and kept[-1].lstrip().startswith(">"):
                kept.pop()
        return "\n".join(kept).strip()
    except Exception:
        return body or ""


def dedup_key(msg: dict) -> str:
    """Stable dedup key for a message. Uses Message-ID when present; otherwise a
    surrogate hash of from|subject|body — NEVER the empty string (an empty key would
    never dedup, so a Message-ID-less mail would reprocess every poll = poison loop)."""
    mid = (msg.get("message_id") or "").strip()
    if mid:
        return mid
    seed = f"{msg.get('from','')}|{msg.get('subject','')}|{msg.get('body','')}"
    return "sha:" + hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:40]


def _thread_anchor(msg: dict) -> Optional[str]:
    """The thread id the correspondent registry resolves on: the agent's outbound
    Message-ID this reply answers (In-Reply-To preferred; else the last References id)."""
    irt = (msg.get("in_reply_to") or "").strip()
    if irt:
        return irt
    refs = (msg.get("references") or "").split()
    return refs[-1].strip() if refs else None


def build_inbound_message(msg: dict, user_directory: Any) -> Optional[InboundMessage]:
    """Normalized email dict -> InboundMessage. None if no usable sender."""
    addr = parse_from_address(msg.get("from", ""))
    if not addr:
        return None
    # Defense-in-depth: a missing user_directory would raise AttributeError below,
    # which poll_once swallows AFTER marking the message \Seen — silently losing it.
    # Fail LOUD instead so a wiring gap is diagnosable, not invisible mail loss.
    if user_directory is None:
        logger.error(
            "email inbound: no user_directory configured; cannot identify sender %s "
            "(message dropped — check EmailHarness wiring)", addr,
        )
        return None
    text = truncate_quoted_history(msg.get("body", ""))
    user_id = user_directory.resolve_internal(addr, "email")
    source = SessionSource(
        surface_id="email",
        chat_id=addr,
        chat_type="dm",
        thread_id=_thread_anchor(msg),
    )
    message_id = msg.get("message_id")
    return InboundMessage(
        text=text,
        identity=Identity(user_id=user_id, source=source, raw_user_id=addr),
        idempotency_key=str(message_id) if message_id else None,
        reply_to=(msg.get("in_reply_to") or None),
        raw=msg,
    )


async def process_email(
    container: Any,
    msg: dict,
    *,
    dedup: Any,
    user_directory: Any,
    is_chitchat=None,
    now: Optional[float] = None,
) -> Optional[InboundResult]:
    """Dedup (by Message-ID) -> identify -> route. None on redelivery / unusable msg."""
    key = dedup_key(msg)
    if dedup is not None:
        try:
            if dedup.seen(key, now=now):
                logger.debug("email inbound: dropping redelivered message %s", key)
                return None
        except Exception as e:  # fail-open: a dedup fault must not drop a real message
            logger.debug("email dedup check failed (processing anyway): %s", e)

    inbound = build_inbound_message(msg, user_directory)
    if inbound is None:
        return None
    decision = await route_inbound(container, inbound, is_chitchat=is_chitchat)
    return InboundResult(inbound=inbound, decision=decision)
