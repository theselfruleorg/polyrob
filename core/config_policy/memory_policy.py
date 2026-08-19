"""MEMORY_BACKEND default policy (027 rider; extracted from policy.py — the
god-file ratchet caps growth there). One SSOT: backend_factory, doctor, and
policy.embedder_needed all derive their default from here."""

import os


def memory_backend_default(local: bool) -> str:
    """local_vector under the single-user local profile, sqlite on servers."""
    return "local_vector" if local else "sqlite"


def resolved_memory_backend() -> str:
    """The effective backend name: explicit MEMORY_BACKEND wins, else the
    profile default. Lowercased; may be 'none'/'' for the null provider."""
    raw = os.getenv("MEMORY_BACKEND")
    if raw is not None and raw.strip():
        return raw.strip().lower()
    from core.config_policy.policy import local_mode_enabled

    return memory_backend_default(local_mode_enabled())
