"""Bounded operating-contract doc writer (owner-authored operating rules).

A small, owner-authored/agent-proposed document of durable operating
rules/constraints, injected alongside SOUL/SELF/owner-facts each session. It is
read next session as steering context, so it is a prompt-injection
**persistence** vector — guarded with the SAME model as the bounded owner-facts
doc (``core/owner_doc_writer.py``) and the evolving SELF doc
(``core/self_context_writer.py``): tenant+instance confined, anon-blocked,
identity-scanned fail-CLOSED, over-cap ERRORS (never truncates),
quarantine-then-promote, atomic writes, archive-never-delete.

Implemented as a thin ``SelfContextWriter`` subclass: the propose/patch/reject/
promote/scan/atomic-write machinery is the base class's ONE shared body (audit
T4, 2026-07-16 — previously ``propose``/``promote``/archives were copy-pasted
here and could drift from the security gate). Only the target file
(``contract.md``), the char cap (``CONTRACT_DOC_MAX_CHARS``), the review flag
(``CONTRACT_DOC_REQUIRE_REVIEW``), the pending-listing ``kind`` (``contract``)
and the archive filename prefixes differ — all expressed as class attributes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.config_policy import AutonomyConfig
from core.instance import (
    CONTRACT_DOC_MAX_CHARS,
    _CONTRACT_DOC_NAME,
)
from core.self_context_writer import (
    PROVENANCE_AGENT,
    PROVENANCE_BACKGROUND,
    PROVENANCE_USER,
    SelfContextWriteResult,
    SelfContextWriter,
    _NON_USER_AUTHORS,
)


class ContractWriter(SelfContextWriter):
    """Create / patch / promote the bounded operating-contract doc for a tenant."""

    _DOC_KIND = "contract"
    _LOG_LABEL = "contract"
    _MAX_CHARS = CONTRACT_DOC_MAX_CHARS
    _CAP_NOUN = "operating contract"
    _CAP_HINT = "consolidate (keep only durable rules/constraints), then retry"
    _ARCHIVE_PREFIX = "contract"
    # Namespace rejected contract drafts separately from the SELF doc's
    # rejected.<n>.md / owner's owner-rejected.<n>.md so archived provenance
    # is unambiguous.
    _REJECTED_PREFIX = "contract-rejected"

    # --- paths (target contract.md instead of self.md) -----------------------

    def _active_file(self, uid: str) -> Path:
        return self._root(uid) / _CONTRACT_DOC_NAME

    def _pending_file(self, uid: str) -> Path:
        return self._root(uid) / ".pending" / _CONTRACT_DOC_NAME

    # --- review flag (contract-specific) --------------------------------------

    def _resolve_pending(self, created_by: str, pending: Optional[bool]) -> bool:
        if pending is not None:
            base = pending
        else:
            base = AutonomyConfig.contract_doc_require_review()
        if created_by in _NON_USER_AUTHORS:  # a forged author can never auto-activate
            return True
        return base


__all__ = [
    "ContractWriter",
    "SelfContextWriteResult",
    "PROVENANCE_USER",
    "PROVENANCE_AGENT",
    "PROVENANCE_BACKGROUND",
]
