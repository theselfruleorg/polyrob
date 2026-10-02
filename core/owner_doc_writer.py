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
    # ⚠️ The hint used to say only "consolidate". The agent read that as a licence
    # to DELETE an older owner rule to make room, which is exactly how the 09-17
    # and 09-18 enforcement anchors disappeared on 2026-09-20. Tightening the
    # words is the cheap half of the fix; raising OWNER_DOC_MAX_CHARS is the other.
    _CAP_HINT = ("tighten the WORDING of existing facts, then retry. Never delete "
                 "or weaken a standing owner rule to make room — if nothing can be "
                 "tightened, ask the owner which rule to retire (a retired rule moves "
                 "under '## Superseded', dated, and stops counting toward the cap)")
    _ARCHIVE_PREFIX = "owner"
    # Namespace rejected owner drafts separately from the SELF doc's
    # rejected.<n>.md so archived provenance is unambiguous.
    _REJECTED_PREFIX = "owner-rejected"

    # --- paths (target owner.md instead of self.md) --------------------------

    def _active_file(self, uid: str) -> Path:
        return self._root(uid) / _OWNER_DOC_NAME

    def _pending_file(self, uid: str) -> Path:
        return self._root(uid) / ".pending" / _OWNER_DOC_NAME

    # --- 060 WS-6: supersede, never evict -------------------------------------

    def _prepare_body(self, uid: str, body: str, *, pending: Optional[bool],
                      created_by: str, observed_at: Optional[str]) -> str:
        """A rule this write DROPS moves under ``## Superseded``, dated (with its
        successor when one line replaced one line) — it is never evicted. The
        baseline is the same one provenance diffs against (the pending draft for
        a quarantined write, else the active doc). Fail-open to the body as given:
        superseding is bookkeeping, and a fault in it must not block a rule."""
        try:
            from core.doc_claims import carry_superseded, owner_rules_supersede
            if not owner_rules_supersede():
                return body
            quarantine = self._resolve_pending(created_by, pending)
            old = self._provenance_baseline(uid, quarantine=quarantine)
            return carry_superseded(old, body, observed_at=observed_at)
        except Exception:
            return body

    def _capped_len(self, body: str) -> int:
        """The cap counts the ACTIVE rules only; the superseded section has its own
        bound (``doc_claims.SUPERSEDED_MAX_CHARS``) and is never injected."""
        try:
            from core.doc_claims import owner_rules_supersede, split_superseded
            if owner_rules_supersede():
                return len(split_superseded(body)[0])
        except Exception:
            pass
        return len(body)

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
