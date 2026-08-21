"""Reason-specific, owner-facing phrases for the LLM-outage notice (T1.2).

Raw model/provider text must never reach the user; POLYROB previously had
exactly one generic notice (``core.surfaces.llm_outage_notice.OUTAGE_NOTICE_TEXT``).
This module adds a short, actionable sentence for WHY the run died on top of that
existing rail, sourced from the same structured taxonomy SSOT
(``core.error_classifier.classify_text`` / ``FailoverReason``) the outage
classifier (``looks_like_llm_outage``) already uses to decide WHETHER to notify.

Deliberately covers only the outage-class reason set ``looks_like_llm_outage``
gates on (CREDIT_DEATH, AUTH_PERMANENT, PROVIDER_EXHAUSTED); every other reason
maps to None, leaving the generic/legacy notice text to stand alone. The phrases
are fixed sentences only — never built from the classified input — so no raw
provider/model text (a 402 body can be ~2KB of JSON) can leak through this seam.
"""
from __future__ import annotations

from typing import Optional

from core.error_classifier import FailoverReason, classify_text

_REASON_PHRASES = {
    FailoverReason.CREDIT_DEATH: (
        "The model provider reports the account is out of credits — top up or "
        "switch provider, then send another message."
    ),
    FailoverReason.AUTH_PERMANENT: (
        "The provider API key was rejected (invalid or deactivated) — fix the "
        "key configuration, then send another message."
    ),
    FailoverReason.PROVIDER_EXHAUSTED: (
        "All configured model providers failed — check provider status/keys, "
        "then send another message."
    ),
}


def notice_for(reason: FailoverReason) -> Optional[str]:
    """One short owner-facing sentence for an outage-class reason; None for
    every other ``FailoverReason``."""
    return _REASON_PHRASES.get(reason)


def notice_for_texts(*texts: Optional[str]) -> Optional[str]:
    """Classify each text (same SSOT + iteration order as
    ``llm_outage_notice.looks_like_llm_outage``) and return the phrase for the
    FIRST outage-class reason found; ``None`` if none classify as an outage.
    Never returns raw input text — only one of the fixed phrases above.
    """
    for text in texts:
        if not text:
            continue
        phrase = notice_for(classify_text(text))
        if phrase:
            return phrase
    return None
