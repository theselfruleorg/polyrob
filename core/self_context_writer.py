"""Evolving SELF identity writer (polyrob C-write.3).

The agent-writable half of the identity layer. A SELF doc
(``identity/{instance}/user_{uid}/self.md``) is read next session as authoritative
self-definition, so it is a prompt-injection **persistence** vector — guarded with a
*stricter* version of the writable-skills model:

1. **Tenant + instance confined** — writes go ONLY under
   ``identity/{instance_id}/user_{uid}/``; ids are sanitized into the path.
2. **Anon-blocked** — empty/blank ``user_id`` is refused.
3. **Identity-scanned fail-CLOSED** — ``is_identity_suspicious`` (self-voice
   subversion + invisible-unicode + base instruction-override) rejects before
   persist; a *raising* scanner also rejects.
4. **Over-cap ERRORS** (Hermes parity) — an over-cap ``propose`` returns an
   actionable "consolidate then retry" error instead of silently truncating.
5. **Quarantined** — a normal author follows ``SELF_CONTEXT_REQUIRE_REVIEW``; a
   forged (sub-agent / self-wake / background-review) turn is **always** ``.pending``
   and can **never** patch/promote an active doc.
6. **Atomic** — temp-file + ``os.replace``.
7. **Archive-never-delete** — an overwritten active doc is backed up to
   ``.archived/self.<n>.md`` (recoverable; the drift/rollback guard).

Promotion of a ``.pending`` draft to active is the owner-review gate.

The SOUL tier (``identity/identity.md`` / ``operating.md``) is intentionally NOT
reachable here — it stays operator-write-only and frozen.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import List, Optional

from core.instance import (
    DEFAULT_INSTANCE_ID,
    SELF_DOC_MAX_CHARS,
    is_safe_tenant_id,
    self_tier_root,
    _SELF_DOC_NAME,
)

logger = logging.getLogger(__name__)

PROVENANCE_USER = "user"
PROVENANCE_AGENT = "agent"
PROVENANCE_BACKGROUND = "background_review"
# Authors whose writes must NEVER auto-activate or touch an active doc, regardless of
# the review flag — a forged (self-wake / background-review / sub-agent) turn.
_NON_USER_AUTHORS = frozenset({PROVENANCE_BACKGROUND})


class SelfContextWriteResult:
    def __init__(self, ok: bool, *, errors: Optional[List[str]] = None,
                 pending: bool = False, path: Optional[str] = None):
        self.ok = ok
        self.errors = errors or []
        self.pending = pending
        self.path = path

    def __repr__(self) -> str:
        state = "pending" if self.pending else ("ok" if self.ok else "rejected")
        return f"<SelfContextWriteResult {state} errors={self.errors}>"


class SelfContextWriter:
    """Create / patch / promote the evolving SELF doc for a tenant."""

    def __init__(self, home_dir: Path | str, instance_id: str = DEFAULT_INSTANCE_ID):
        self.home_dir = Path(home_dir)
        self.instance_id = instance_id or DEFAULT_INSTANCE_ID

    # --- paths ---------------------------------------------------------------

    def _root(self, uid: str) -> Path:
        return self_tier_root(self.home_dir, uid, self.instance_id)

    def _active_file(self, uid: str) -> Path:
        return self._root(uid) / _SELF_DOC_NAME

    def _pending_file(self, uid: str) -> Path:
        return self._root(uid) / ".pending" / _SELF_DOC_NAME

    @staticmethod
    def _require_user(user_id: Optional[str]) -> Optional[str]:
        """Return a clean, path-safe user_id or None.

        Refuses empty/anon ids AND ids with path-dangerous characters (rather than
        sanitizing them, which could collapse two distinct tenants into one dir).
        """
        if user_id is None:
            return None
        uid = str(user_id).strip()
        if not uid or not is_safe_tenant_id(uid):
            return None
        return uid

    def _resolve_pending(self, created_by: str, pending: Optional[bool]) -> bool:
        if pending is not None:
            base = pending
        else:
            from agents.task.constants import AutonomyConfig
            base = AutonomyConfig.self_context_require_review()
        # Hard rule: a forged author can NEVER auto-activate.
        if created_by in _NON_USER_AUTHORS:
            return True
        return base

    # --- public API ----------------------------------------------------------

    def read(self, user_id: str) -> str:
        """Return the live ACTIVE self.md text (so the agent can self-consolidate)."""
        uid = self._require_user(user_id)
        if uid is None:
            return ""
        f = self._active_file(uid)
        try:
            return f.read_text(encoding="utf-8") if f.is_file() else ""
        except Exception:
            return ""

    def propose(self, content: str, *, user_id: str,
                created_by: str = PROVENANCE_AGENT,
                pending: Optional[bool] = None) -> SelfContextWriteResult:
        """Author/replace the SELF doc (validated, scanned, atomically written)."""
        uid = self._require_user(user_id)
        if uid is None:
            return SelfContextWriteResult(False, errors=["empty user_id refused (tenant scope)"])

        body = content or ""
        # Over-cap ERRORS (Hermes) — never silently truncate the SELF doc.
        if len(body) > SELF_DOC_MAX_CHARS:
            return SelfContextWriteResult(False, errors=[
                f"self-context is {len(body)}/{SELF_DOC_MAX_CHARS} chars — consolidate "
                f"(merge/shorten overlapping notes), then retry"])
        if not body.strip():
            return SelfContextWriteResult(False, errors=["empty content"])

        # Identity scan, fail-CLOSED on EVERY failure mode: a flagged body, a raising
        # scan, OR an unavailable scanner all reject (an identity write must never slip
        # past a missing/broken guard — matches load_self_doc's read-side posture).
        try:
            from modules.memory.task.threat_scan import is_identity_suspicious
        except Exception as e:
            logger.warning("self-context write rejected (scanner unavailable, fail-closed): %s: %s", uid, e)
            return SelfContextWriteResult(False, errors=["identity scanner unavailable (rejected)"])
        try:
            flagged = is_identity_suspicious(body)
        except Exception as e:
            logger.warning("self-context write rejected (scan error, fail-closed): %s: %s", uid, e)
            return SelfContextWriteResult(False, errors=["identity scan error (rejected)"])
        if flagged:
            logger.warning("self-context write rejected (identity scan): %s", uid)
            return SelfContextWriteResult(False, errors=["content failed identity safety scan"])

        quarantine = self._resolve_pending(created_by, pending)
        target = self._pending_file(uid) if quarantine else self._active_file(uid)
        try:
            if not quarantine:
                self._archive_existing(uid)
            self._atomic_write(target, body)
            logger.info("self-context %s written (%s)", uid, "pending" if quarantine else "active")
            return SelfContextWriteResult(True, pending=quarantine, path=str(target))
        except Exception as e:
            logger.error("self-context write failed for %s: %s", uid, e, exc_info=True)
            return SelfContextWriteResult(False, errors=[f"write failed: {e}"])

    def patch(self, *, user_id: str, old_string: str, new_string: str,
              replace_all: bool = False, created_by: str = PROVENANCE_AGENT,
              pending: Optional[bool] = None) -> SelfContextWriteResult:
        """Exact-match edit of an existing SELF doc, re-validated + re-scanned."""
        uid = self._require_user(user_id)
        if uid is None:
            return SelfContextWriteResult(False, errors=["empty user_id refused"])

        active = self._active_file(uid)
        pending_f = self._pending_file(uid)
        # Always prefer an existing PENDING draft (so a patch refines the proposal in
        # flight rather than silently discarding it by editing the active doc); fall
        # back to the active doc only when there is no pending draft.
        target_is_pending = pending_f.is_file()
        src = pending_f if target_is_pending else active
        if not src.is_file():
            return SelfContextWriteResult(False, errors=["no self-context doc to patch"])

        # A forged turn may refine its OWN pending draft but NEVER an active doc
        # (when no pending exists, src is the active doc → block the forged author).
        if created_by in _NON_USER_AUTHORS and not target_is_pending:
            return SelfContextWriteResult(False, errors=["a background turn cannot patch the active self-context"])

        try:
            current = src.read_text(encoding="utf-8")
        except Exception as e:
            return SelfContextWriteResult(False, errors=[f"read failed: {e}"])

        count = current.count(old_string)
        if count == 0:
            return SelfContextWriteResult(False, errors=["old_string not found"])
        if count > 1 and not replace_all:
            return SelfContextWriteResult(False, errors=[f"old_string occurs {count}× — pass replace_all=true"])
        updated = (current.replace(old_string, new_string) if replace_all
                   else current.replace(old_string, new_string, 1))

        # Re-run the full gate; preserve the doc's pending/active state.
        return self.propose(updated, user_id=uid, created_by=created_by,
                            pending=pending if pending is not None else target_is_pending)

    def list_pending(self, user_id: str) -> Optional[dict]:
        """Return a summary of the tenant's pending self-doc draft, or None.

        Used by the owner-facing transparency surface to enumerate what the agent
        has proposed but not yet had promoted. A tenant has at most one pending
        self.md draft.
        """
        uid = self._require_user(user_id)
        if uid is None:
            return None
        pending_f = self._pending_file(uid)
        if not pending_f.is_file():
            return None
        try:
            body = pending_f.read_text(encoding="utf-8")
        except Exception:
            return None
        preview = body.strip().replace("\n", " ")
        if len(preview) > 280:
            preview = preview[:277] + "…"
        return {
            "kind": "self_context",
            "user_id": uid,
            "preview": preview,
            "chars": len(body),
            "path": str(pending_f),
        }

    def reject(self, *, user_id: str) -> SelfContextWriteResult:
        """Discard a pending self-doc draft (owner rejects the proposal).

        Archive-never-delete: the rejected draft is backed up to
        ``.archived/rejected.<n>.md`` before removal so a decision is recoverable.
        The active doc is never touched.
        """
        uid = self._require_user(user_id)
        if uid is None:
            return SelfContextWriteResult(False, errors=["empty user_id refused"])
        pending_f = self._pending_file(uid)
        if not pending_f.is_file():
            return SelfContextWriteResult(False, errors=["no pending self-context to reject"])
        try:
            self._archive_pending(uid, pending_f)
            os.remove(str(pending_f))
            logger.info("self-context %s pending draft rejected (archived)", uid)
            return SelfContextWriteResult(True, path=str(pending_f))
        except Exception as e:
            logger.error("self-context reject failed for %s: %s", uid, e, exc_info=True)
            return SelfContextWriteResult(False, errors=[f"reject failed: {e}"])

    def promote(self, *, user_id: str) -> SelfContextWriteResult:
        """Move the .pending self.md into active use (the owner-review gate)."""
        uid = self._require_user(user_id)
        if uid is None:
            return SelfContextWriteResult(False, errors=["empty user_id refused"])
        pending_f = self._pending_file(uid)
        if not pending_f.is_file():
            return SelfContextWriteResult(False, errors=["no pending self-context to promote"])
        try:
            content = pending_f.read_text(encoding="utf-8")
        except Exception as e:
            return SelfContextWriteResult(False, errors=[f"read failed: {e}"])
        # Promotion is owner-initiated → user provenance, not pending.
        res = self.propose(content, user_id=uid, created_by=PROVENANCE_USER, pending=False)
        if res.ok and not res.pending:
            try:
                os.remove(str(pending_f))
            except OSError:
                pass
        return res

    # --- internals -----------------------------------------------------------

    def _archive_existing(self, uid: str) -> None:
        """Back up the current active doc before overwrite (recoverable rollback)."""
        active = self._active_file(uid)
        if not active.is_file():
            return
        archive_dir = self._root(uid) / ".archived"
        archive_dir.mkdir(parents=True, exist_ok=True)
        # monotonic index (no wall-clock dependency); pick the next free slot.
        n = 0
        while (archive_dir / f"self.{n}.md").exists():
            n += 1
        try:
            import shutil
            shutil.copy2(str(active), str(archive_dir / f"self.{n}.md"))
        except Exception:
            pass  # archival is best-effort; never block a write on it

    def _archive_pending(self, uid: str, pending_f: Path) -> None:
        """Back up a rejected pending draft (best-effort, recoverable rollback)."""
        archive_dir = self._root(uid) / ".archived"
        archive_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        while (archive_dir / f"rejected.{n}.md").exists():
            n += 1
        try:
            import shutil
            shutil.copy2(str(pending_f), str(archive_dir / f"rejected.{n}.md"))
        except Exception:
            pass  # archival is best-effort; never block a reject on it

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, str(path))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


__all__ = [
    "SelfContextWriter",
    "SelfContextWriteResult",
    "PROVENANCE_USER",
    "PROVENANCE_AGENT",
    "PROVENANCE_BACKGROUND",
]
