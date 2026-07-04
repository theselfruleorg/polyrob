"""Lightweight prompt-injection scanner for memory writes (opt-in via MEMORY_THREAT_SCAN).

Pure + deterministic: no LLM, no network. Conservative pattern set — meant to catch
the obvious 'instruction-override' class of injected text before it is persisted as a
recallable finding, not to be a complete IDS.
"""
import re

_PATTERNS = [
    # Instruction-override (high precision; keep broad)
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|your)\s+(instructions|rules)",
    # System-prompt leak/override — require an imperative verb (or a verbatim qualifier)
    # NEAR "system prompt" so a plain mention ("exposes its system prompt via /debug")
    # is NOT flagged (MED-5: was a bare \bsystem\s*prompt\b → silent data loss).
    r"(reveal|leak|print|dump|output|repeat|ignore|override)\b[^.\n]{0,40}\bsystem\s*prompt\b",
    r"\bsystem\s*prompt\b[^.\n]{0,40}\b(verbatim|above|word\s+for\s+word)\b",
    # Jailbreak role-reset — require a jailbreak continuation, not a bare "you are now ...".
    r"you\s+are\s+now\s+(a\s+|an\s+|in\s+)?(dan\b|jailbroken|unrestricted|uncensored|developer\s+mode|free\s+(of|from))",
    r"you\s+are\s+now\s+able\s+to\s+(ignore|bypass|disregard)",
    # "enter developer/god/debug mode" (not a bare "developer mode" product reference).
    r"\benter\s+(developer|god|debug)\s+mode\b",
    # Explicit reveal of hidden/system material.
    r"reveal\s+(the\s+)?(system|hidden)\s+(prompt|instructions|rules|message)",
    # Fake role tags.
    r"</?\s*(system|assistant)\s*>",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in _PATTERNS]


def is_suspicious(text: str) -> bool:
    """True if `text` matches a known instruction-override / prompt-leak pattern."""
    if not text:
        return False
    return any(rx.search(text) for rx in _COMPILED)


# --- Identity-subversion scanner (polyrob C-write.2) -------------------------
# ADDITIVE + isolated: used ONLY on the agent-writable identity/SELF write path.
# A SELF doc is read as authoritative self-definition, so it needs to reject
# *self-voice subversion* (rewriting who the agent is / its boundaries) on top of
# the base instruction-override patterns. Does NOT change `is_suspicious`.
_IDENTITY_PATTERNS = [
    # Self-voice / persona reset.
    r"you\s+are\s+now\s+(an?\s+|in\s+)?(unrestricted|jailbroken|uncensored|different|new)\b",
    r"forget\s+(your|the|all)\s+(identity|persona|soul|boundaries|rules|instructions)",
    r"your\s+(new|real|true)\s+(name|identity|purpose|persona)\s+is\b",
    r"ignore\s+your\s+(soul|identity|operating|persona)\b",
    r"disregard\s+your\s+(boundaries|safety|constraints|operating|identity)\b",
    r"from\s+now\s+on[^.\n]{0,40}\b(disregard|ignore|bypass|drop)\b[^.\n]{0,40}\b(boundaries|rules|safety|constraints|identity)\b",
    # Imperative "always comply regardless" subversion.
    r"\balways\s+comply\b[^.\n]{0,30}\bregardless\b",
]
_IDENTITY_COMPILED = [re.compile(p, re.IGNORECASE) for p in _IDENTITY_PATTERNS]

# Invisible / bidirectional control characters used to smuggle hidden instructions
# past a human reviewer (zero-width, bidi overrides, BOM, word-joiner). NOTE: the
# zero-width JOINER (U+200D) is deliberately EXCLUDED — it is heavily used in
# legitimate emoji sequences (e.g. 👨‍💻), so blocking it would false-positive on
# normal prose. The genuine smuggling vectors (zero-width SPACE, bidi overrides) are
# kept.
_INVISIBLE_CHARS = (
    "​‌‎‏"     # zero-width space/non-joiner + LRM/RLM (NOT the joiner U+200D)
    "‪‫‬‭‮"   # bidi embeddings/overrides
    "⁠﻿"                      # word-joiner, BOM/zero-width-no-break
    "­"                            # soft hyphen
)


def has_invisible_unicode(text: str) -> bool:
    """True if `text` contains zero-width / bidi-control characters."""
    if not text:
        return False
    return any(ch in _INVISIBLE_CHARS for ch in text)


def is_identity_suspicious(text: str) -> bool:
    """Stricter scan for the identity/SELF write path.

    Composes the base `is_suspicious` (instruction-override / prompt-leak) with
    identity-subversion patterns and invisible-unicode detection. Fail-closed:
    callers reject the write when this returns True (or when it RAISES).
    """
    if not text:
        return False
    if has_invisible_unicode(text):
        return True
    if is_suspicious(text):
        return True
    return any(rx.search(text) for rx in _IDENTITY_COMPILED)
