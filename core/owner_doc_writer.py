"""Bounded owner-facts doc writer (USER.md-equivalent).

A small, agent-maintained document of durable OWNER facts/preferences, injected
alongside SOUL/SELF each session. It is read next session as steering context, so
it is a prompt-injection **persistence** vector — guarded with the SAME model as
the evolving SELF doc (``core/self_context_writer.py``): tenant+instance confined,
anon-blocked, identity-scanned fail-CLOSED, over-cap ERRORS (never truncates),
quarantine-then-promote, atomic writes, archive-never-delete.

Implemented as a thin ``SelfContextWriter`` subclass: the propose/patch/reject/
promote/scan/atomic-write machinery is the base class's ONE shared body (audit
T4, 2026-07-16 — previously ``propose``/``promote``/archives were copy-pasted
here and could drift from the security gate). Only the target file
(``owner.md``), the char cap (``OWNER_DOC_MAX_CHARS`` — terser than SELF), the
review flag (``OWNER_DOC_REQUIRE_REVIEW``), the pending-listing ``kind``
(``owner_doc``) and the archive filename prefixes differ — all expressed as
class attributes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.config_policy import AutonomyConfig
from core.instance import (
    OWNER_DOC_MAX_CHARS,
    _OWNER_DOC_NAME,
)
from core.self_context_writer import (
    PROVENANCE_AGENT,
    PROVENANCE_BACKGROUND,
    PROVENANCE_USER,
    SelfContextWriteResult,
    SelfContextWriter,
    _NON_USER_AUTHORS,
)


class OwnerDocWriter(SelfContextWriter):
    """Create / patch / promote the bounded owner-facts doc for a tenant."""

    _DOC_KIND = "owner_doc"
    _LOG_LABEL = "owner-doc"
    _MAX_CHARS = OWNER_DOC_MAX_CHARS
    _CAP_NOUN = "owner-facts doc"
    _CAP_HINT = "consolidate (keep only durable facts/preferences), then retry"
    _ARCHIVE_PREFIX = "owner"
    # Namespace rejected owner drafts separately from the SELF doc's
    # rejected.<n>.md so archived provenance is unambiguous.
    _REJECTED_PREFIX = "owner-rejected"

    # --- paths (target owner.md instead of self.md) --------------------------

    def _active_file(self, uid: str) -> Path:
        return self._root(uid) / _OWNER_DOC_NAME

    def _pending_file(self, uid: str) -> Path:
        return self._root(uid) / ".pending" / _OWNER_DOC_NAME

    # --- review flag (owner-doc-specific) ------------------------------------

    def _resolve_pending(self, created_by: str, pending: Optional[bool]) -> bool:
        if pending is not None:
            base = pending
        else:
            base = AutonomyConfig.owner_doc_require_review()
        if created_by in _NON_USER_AUTHORS:  # a forged author can never auto-activate
            return True
        return base


__all__ = [
    "OwnerDocWriter",
    "SelfContextWriteResult",
    "PROVENANCE_USER",
    "PROVENANCE_AGENT",
    "PROVENANCE_BACKGROUND",
]
