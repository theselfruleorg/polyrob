"""Bounded owner-facts doc writer (USER.md-equivalent).

A small, agent-maintained document of durable OWNER facts/preferences, injected
alongside SOUL/SELF each session. It is read next session as steering context, so
it is a prompt-injection **persistence** vector — guarded with the SAME model as
the evolving SELF doc (``core/self_context_writer.py``): tenant+instance confined,
anon-blocked, identity-scanned fail-CLOSED, over-cap ERRORS (never truncates),
quarantine-then-promote, atomic writes, archive-never-delete.

Implemented as a thin ``SelfContextWriter`` subclass so the patch/reject/promote/
atomic-write machinery is shared verbatim; only the target file (``owner.md``),
the char cap (``OWNER_DOC_MAX_CHARS`` — terser than SELF), the review flag
(``OWNER_DOC_REQUIRE_REVIEW``), the pending-listing ``kind`` (``owner_doc``) and
the archive filenames differ.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from core.instance import (
    DEFAULT_INSTANCE_ID,
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

logger = logging.getLogger(__name__)


class OwnerDocWriter(SelfContextWriter):
    """Create / patch / promote the bounded owner-facts doc for a tenant."""

    _DOC_KIND = "owner_doc"

    def __init__(self, home_dir: Path | str, instance_id: str = DEFAULT_INSTANCE_ID):
        super().__init__(home_dir, instance_id)

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
            from agents.task.constants import AutonomyConfig
            base = AutonomyConfig.owner_doc_require_review()
        if created_by in _NON_USER_AUTHORS:  # a forged author can never auto-activate
            return True
        return base

    # --- propose (owner cap + owner archive) ---------------------------------

    def propose(self, content: str, *, user_id: str,
                created_by: str = PROVENANCE_AGENT,
                pending: Optional[bool] = None) -> SelfContextWriteResult:
        """Author/replace the owner-facts doc (validated, scanned, atomic)."""
        uid = self._require_user(user_id)
        if uid is None:
            return SelfContextWriteResult(False, errors=["empty user_id refused (tenant scope)"])

        body = content or ""
        if len(body) > OWNER_DOC_MAX_CHARS:
            return SelfContextWriteResult(False, errors=[
                f"owner-facts doc is {len(body)}/{OWNER_DOC_MAX_CHARS} chars — consolidate "
                f"(keep only durable facts/preferences), then retry"])
        if not body.strip():
            return SelfContextWriteResult(False, errors=["empty content"])

        # Identity scan, fail-CLOSED on every failure mode (flagged / raising / missing).
        try:
            from modules.memory.task.threat_scan import is_identity_suspicious
        except Exception as e:
            logger.warning("owner-doc write rejected (scanner unavailable, fail-closed): %s: %s", uid, e)
            return SelfContextWriteResult(False, errors=["identity scanner unavailable (rejected)"])
        try:
            flagged = is_identity_suspicious(body)
        except Exception as e:
            logger.warning("owner-doc write rejected (scan error, fail-closed): %s: %s", uid, e)
            return SelfContextWriteResult(False, errors=["identity scan error (rejected)"])
        if flagged:
            logger.warning("owner-doc write rejected (identity scan): %s", uid)
            return SelfContextWriteResult(False, errors=["content failed identity safety scan"])

        quarantine = self._resolve_pending(created_by, pending)
        target = self._pending_file(uid) if quarantine else self._active_file(uid)
        try:
            if not quarantine:
                self._archive_existing(uid)
            self._atomic_write(target, body)
            logger.info("owner-doc %s written (%s)", uid, "pending" if quarantine else "active")
            return SelfContextWriteResult(True, pending=quarantine, path=str(target))
        except Exception as e:
            logger.error("owner-doc write failed for %s: %s", uid, e, exc_info=True)
            return SelfContextWriteResult(False, errors=[f"write failed: {e}"])

    def promote(self, *, user_id: str) -> SelfContextWriteResult:
        """Move the .pending owner.md into active use (the owner-review gate)."""
        uid = self._require_user(user_id)
        if uid is None:
            return SelfContextWriteResult(False, errors=["empty user_id refused"])
        pending_f = self._pending_file(uid)
        if not pending_f.is_file():
            return SelfContextWriteResult(False, errors=["no pending owner-facts doc to promote"])
        try:
            content = pending_f.read_text(encoding="utf-8")
        except Exception as e:
            return SelfContextWriteResult(False, errors=[f"read failed: {e}"])
        res = self.propose(content, user_id=uid, created_by=PROVENANCE_USER, pending=False)
        if res.ok and not res.pending:
            import os
            try:
                os.remove(str(pending_f))
            except OSError:
                pass
        return res

    def list_pending(self, user_id: str) -> Optional[dict]:
        d = super().list_pending(user_id)
        if d is not None:
            d["kind"] = self._DOC_KIND
        return d

    # --- archive filenames (owner.<n>.md instead of self.<n>.md) -------------

    def _archive_existing(self, uid: str) -> None:
        active = self._active_file(uid)
        if not active.is_file():
            return
        archive_dir = self._root(uid) / ".archived"
        archive_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        while (archive_dir / f"owner.{n}.md").exists():
            n += 1
        try:
            shutil.copy2(str(active), str(archive_dir / f"owner.{n}.md"))
        except Exception:
            pass  # best-effort

    def _archive_pending(self, uid: str, pending_f: Path) -> None:
        # Namespace rejected owner drafts separately from the SELF doc's
        # rejected.<n>.md so archived provenance is unambiguous.
        archive_dir = self._root(uid) / ".archived"
        archive_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        while (archive_dir / f"owner-rejected.{n}.md").exists():
            n += 1
        try:
            shutil.copy2(str(pending_f), str(archive_dir / f"owner-rejected.{n}.md"))
        except Exception:
            pass  # best-effort


__all__ = [
    "OwnerDocWriter",
    "SelfContextWriteResult",
    "PROVENANCE_USER",
    "PROVENANCE_AGENT",
    "PROVENANCE_BACKGROUND",
]
