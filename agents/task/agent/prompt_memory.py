"""The <memory-system> section of the system prompt.

Split out of ``prompts.py`` (god-file ratchet) when the coherency review
(2026-10-13, E-A11) gated the "cross-session recall" claim on the configured
backend: with ``MEMORY_BACKEND=none`` no provider registers, so the prompt must
not tell the model it remembers earlier sessions.
"""
from __future__ import annotations

import os

_LONG_TERM_ON = """- Long-term: Cross-session recall finds relevant findings from ANY previous step, matched by
  keyword (default FTS5 backend) — semantic/embedding matching only applies if the
  local_vector memory backend is enabled
"""

_LONG_TERM_OFF = """- Long-term: OFF on this deployment (MEMORY_BACKEND=none) — nothing from an earlier
  session is recalled. Do not claim you remember a past session; say recall is off
"""

_REST = """- Organized: By work phase (discovery, collection, processing, documentation)
- Persistent: checkpointed periodically, survives session restarts"""

_RECENT_ACTIVITY = (
    "\n\nTo answer questions about your OWN past activity or runs ('what did I do', "
    "'what ran since X'), call `recent_activity` — do NOT inspect the filesystem or "
    "rely on self-notes."
)


def cross_session_recall_on() -> bool:
    """Is a cross-session memory backend configured?

    Mirrors ``modules.memory.backend_factory``: only ``sqlite`` / ``local_vector``
    register a provider; ``none`` / ``off`` / an explicit empty value register
    nothing. Fail-open to True (the legacy text) when the policy cannot be read.
    """
    try:
        raw = os.getenv("MEMORY_BACKEND")
        if raw is None:
            from core.config_policy.memory_policy import resolved_memory_backend
            raw = resolved_memory_backend()
        return raw.strip().lower() in ("sqlite", "local_vector")
    except Exception:
        return True


def _recent_activity_registerable() -> bool:
    """The `recent_activity` steering sentence is gated on the SAME rule that
    gates the action's own registration (``AutonomyConfig.episodic_memory_enabled()``
    AND an external memory provider active — tools/controller/action_registration.py
    ``_register_recent_activity_action``). Fail-open to OMITTING the sentence when
    the flag cannot be resolved, so a broken import never advertises a missing tool;
    fail-open to the flag-only value when the registry lookup itself raises (SK-F5).
    """
    try:
        from core.config_policy import AutonomyConfig
        episodic_on = AutonomyConfig.episodic_memory_enabled()
    except Exception:
        episodic_on = False
    try:
        from modules.memory.registry import get_memory_registry
        provider = get_memory_registry().active()
        episodic_on = episodic_on and provider is not None and getattr(provider, "is_external", False)
    except Exception:
        pass
    return bool(episodic_on)


def memory_system_content() -> str:
    """The <memory-system> body."""
    long_term = _LONG_TERM_ON if cross_session_recall_on() else _LONG_TERM_OFF
    base = ("Your Memory Types:\n"
            "- Short-term: recent message exchanges for immediate context\n"
            + long_term + _REST)
    if _recent_activity_registerable():
        base += _RECENT_ACTIVITY
    return base
