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
4. **Over-cap ERRORS** — an over-cap ``propose`` returns an
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
from datetime import datetime, timezone
import tempfile
from pathlib import Path
from typing import List, Optional

from core.config_policy import AutonomyConfig
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

    # Template-method knobs — subclasses (ContractWriter/OwnerDocWriter) override
    # these instead of copy-pasting the propose/promote/archive bodies (audit T4,
    # 2026-07-16: the identity write gate is a SECURITY guard; three drifting
    # copies were a bypass waiting to happen).
    _DOC_KIND = "self_context"
    _LOG_LABEL = "self-context"
    _MAX_CHARS = SELF_DOC_MAX_CHARS
    _CAP_NOUN = "self-context"
    _CAP_HINT = "consolidate (merge/shorten overlapping notes), then retry"
    _ARCHIVE_PREFIX = "self"
    _REJECTED_PREFIX = "rejected"

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

    def _scan_body(self, uid: str, body: str) -> Optional[SelfContextWriteResult]:
        """Identity scan, fail-CLOSED on EVERY failure mode: a flagged body, a raising
        scan, OR an unavailable scanner all reject (an identity write must never slip
        past a missing/broken guard — matches load_self_doc's read-side posture).
        Returns a rejection result, or None when the body is clean. This is the ONE
        shared gate for all three identity-doc writers."""
        try:
            from modules.memory.task.threat_scan import is_identity_suspicious
        except Exception as e:
            logger.warning("%s write rejected (scanner unavailable, fail-closed): %s: %s",
                           self._LOG_LABEL, uid, e)
            return SelfContextWriteResult(False, errors=["identity scanner unavailable (rejected)"])
        try:
            flagged = is_identity_suspicious(body)
        except Exception as e:
            logger.warning("%s write rejected (scan error, fail-closed): %s: %s",
                           self._LOG_LABEL, uid, e)
            return SelfContextWriteResult(False, errors=["identity scan error (rejected)"])
        if flagged:
            logger.warning("%s write rejected (identity scan): %s", self._LOG_LABEL, uid)
            return SelfContextWriteResult(False, errors=["content failed identity safety scan"])
        return None

    # --- provenance (057 WS-D) ----------------------------------------------

    def _provenance_baseline(self, uid: str, *, quarantine: bool) -> str:
        """The body a write is being diffed AGAINST.

        A quarantined write refines the pending draft when one exists, else the
        active doc; an immediate write replaces the active doc. Matching
        :meth:`patch`'s own source selection keeps "changed line" meaning the same
        thing on both write paths. Never raises — an unreadable baseline reads as
        empty, which stamps more lines rather than fewer (fail toward provenance).
        """
        try:
            if quarantine:
                pf = self._pending_file(uid)
                if pf.is_file():
                    return pf.read_text(encoding="utf-8")
            af = self._active_file(uid)
            return af.read_text(encoding="utf-8") if af.is_file() else ""
        except Exception:
            return ""

    def _apply_provenance(self, uid: str, body: str, *, pending: Optional[bool],
                          created_by: str, source: Optional[str],
                          observed_at: Optional[str]):
        """Run the claim guard, then stamp. Returns the new body, or a rejection
        ``SelfContextWriteResult`` when the guard trips. Fail-open on an internal
        fault: provenance is metadata, and a broken stamper must never be able to
        block a legitimate identity write."""
        try:
            from core.doc_claims import (claim_provenance_required,
                                         find_unsourced_claims,
                                         stamp_changed_lines,
                                         unsourced_claim_error)
        except Exception:
            return body
        try:
            if not (source or "").strip() and not claim_provenance_required():
                return body
            quarantine = self._resolve_pending(created_by, pending)
            old = self._provenance_baseline(uid, quarantine=quarantine)
            if claim_provenance_required():
                unsourced = find_unsourced_claims(old, body, source)
                if unsourced:
                    logger.info("%s write refused (unsourced claim): %s",
                                self._LOG_LABEL, uid)
                    return SelfContextWriteResult(
                        False, errors=[unsourced_claim_error(unsourced)])
            if not (source or "").strip():
                return body
            return stamp_changed_lines(old, body, source, observed_at)
        except Exception as e:
            logger.debug("%s provenance skipped (fail-open): %s", self._LOG_LABEL, e)
            return body

    def propose(self, content: str, *, user_id: str,
                created_by: str = PROVENANCE_AGENT,
                pending: Optional[bool] = None,
                source: Optional[str] = None,
                observed_at: Optional[str] = None) -> SelfContextWriteResult:
        """Author/replace the doc (validated, scanned, atomically written).

        057 WS-D: ``source``/``observed_at`` attach PROVENANCE. When ``source`` is
        given, every NEW or CHANGED line is rendered with a trailing
        ``[from: <source> <date>]``; unchanged lines are left exactly as they
        were, so revising one sentence never re-dates the document. The stamp is
        part of the body and therefore counts toward the char cap — the cap check
        below runs on the STAMPED text, so an over-cap refusal is honest about
        what will actually be written.

        The claim guard (``DOC_CLAIM_PROVENANCE_REQUIRED``, default OFF) refuses a
        new/changed line that makes a durable claim with no ``source``. It is a
        FORMAT check, not a truth check.
        """
        uid = self._require_user(user_id)
        if uid is None:
            return SelfContextWriteResult(False, errors=["empty user_id refused (tenant scope)"])

        body = content or ""
        prov = self._apply_provenance(uid, body, pending=pending,
                                      created_by=created_by, source=source,
                                      observed_at=observed_at)
        if isinstance(prov, SelfContextWriteResult):
            return prov
        body = prov
        # Over-cap ERRORS — never silently truncate an identity doc.
        if len(body) > self._MAX_CHARS:
            return SelfContextWriteResult(False, errors=[
                f"{self._CAP_NOUN} is {len(body)}/{self._MAX_CHARS} chars — {self._CAP_HINT}"])
        if not body.strip():
            return SelfContextWriteResult(False, errors=["empty content"])

        rejection = self._scan_body(uid, body)
        if rejection is not None:
            return rejection

        quarantine = self._resolve_pending(created_by, pending)
        target = self._pending_file(uid) if quarantine else self._active_file(uid)
        try:
            if not quarantine:
                self._archive_existing(uid)
            self._atomic_write(target, body)
            logger.info("%s %s written (%s)", self._LOG_LABEL, uid,
                        "pending" if quarantine else "active")
            return SelfContextWriteResult(True, pending=quarantine, path=str(target))
        except Exception as e:
            logger.error("%s write failed for %s: %s", self._LOG_LABEL, uid, e, exc_info=True)
            return SelfContextWriteResult(False, errors=[f"write failed: {e}"])

    def patch(self, *, user_id: str, old_string: str, new_string: str,
              replace_all: bool = False, created_by: str = PROVENANCE_AGENT,
              pending: Optional[bool] = None,
              source: Optional[str] = None,
              observed_at: Optional[str] = None) -> SelfContextWriteResult:
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
        # 057 WS-D: provenance rides through, and the baseline propose diffs
        # against is the SAME `src` we just read — so only the patched line is
        # new/changed, which is exactly the line that gets stamped.
        return self.propose(updated, user_id=uid, created_by=created_by,
                            pending=pending if pending is not None else target_is_pending,
                            source=source, observed_at=observed_at)

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
            "kind": self._DOC_KIND,
            "user_id": uid,
            "preview": preview,
            "chars": len(body),
            "path": str(pending_f),
        }

    def read_active(self, user_id: str) -> str:
        """The ACTIVE doc's raw text, or "" (035 P1-11 needs it for the change
        summary; the ``core.instance`` loaders apply a [BLOCKED] placeholder and
        a load cap, which would corrupt a diff)."""
        uid = self._require_user(user_id)
        if uid is None:
            return ""
        try:
            f = self._active_file(uid)
            return f.read_text(encoding="utf-8") if f.is_file() else ""
        except OSError:
            return ""

    def retire_pending(self, *, user_id: str) -> bool:
        """Archive + remove a draft the ACTIVE doc has now superseded (035 P1-6).

        Leaving it behind would show the owner a `/pending` proposal that is
        already law. Archive-never-delete still applies. Fail-open.
        """
        uid = self._require_user(user_id)
        if uid is None:
            return False
        pending_f = self._pending_file(uid)
        if not pending_f.is_file():
            return False
        try:
            self._archive_pending(uid, pending_f)
            os.remove(str(pending_f))
            return True
        except OSError:
            logger.debug("%s %s: superseded draft not removed", self._LOG_LABEL, uid)
            return False

    def apply_now(self, content: str, *, user_id: str,
                  created_by: str = PROVENANCE_AGENT,
                  source: Optional[str] = None,
                  observed_at: Optional[str] = None) -> SelfContextWriteResult:
        """Write the ACTIVE doc and retire any draft it supersedes (035 P1-6).

        Routes through :meth:`propose` with ``pending=False``, so the full gate
        still runs — cap, safety scan, archive-before-overwrite, atomic write —
        and ``_resolve_pending``'s hard rule still quarantines a non-user author
        regardless of what this asks for. That layering is deliberate: the
        immediacy decision belongs to the CALLER (is this an owner turn?), the
        forged-author refusal belongs HERE, and neither can be bypassed by the
        other.

        Retiring the superseded draft matters for honesty: leaving it behind
        would show the owner a `/pending` proposal that is already law.
        """
        uid = self._require_user(user_id)
        if uid is None:
            return SelfContextWriteResult(False, errors=["empty user_id refused"])
        res = self.propose(content, user_id=uid, created_by=created_by, pending=False,
                           source=source, observed_at=observed_at)
        if res.ok and not res.pending:
            self.retire_pending(user_id=uid)
        return res

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
            return SelfContextWriteResult(
                False, errors=[f"no pending {self._CAP_NOUN} to reject"])
        try:
            self._archive_pending(uid, pending_f)
            os.remove(str(pending_f))
            logger.info("%s %s pending draft rejected (archived)", self._LOG_LABEL, uid)
            return SelfContextWriteResult(True, path=str(pending_f))
        except Exception as e:
            logger.error("%s reject failed for %s: %s", self._LOG_LABEL, uid, e, exc_info=True)
            return SelfContextWriteResult(False, errors=[f"reject failed: {e}"])

    def promote(self, *, user_id: str) -> SelfContextWriteResult:
        """Move the .pending doc into active use (the owner-review gate)."""
        uid = self._require_user(user_id)
        if uid is None:
            return SelfContextWriteResult(False, errors=["empty user_id refused"])
        pending_f = self._pending_file(uid)
        if not pending_f.is_file():
            return SelfContextWriteResult(
                False, errors=[f"no pending {self._CAP_NOUN} to promote"])
        try:
            content = pending_f.read_text(encoding="utf-8")
        except Exception as e:
            return SelfContextWriteResult(False, errors=[f"read failed: {e}"])
        # Promotion is owner-initiated → user provenance, not pending. The
        # owner's approval IS the provenance for any line the draft left bare
        # (stamped lines keep their own stamp) — without a source= here the 057
        # claim guard refused every sourced draft on promote (2026-09-20 07:23Z).
        res = self.propose(content, user_id=uid, created_by=PROVENANCE_USER, pending=False,
                           source="owner approved")
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
        while (archive_dir / f"{self._ARCHIVE_PREFIX}.{n}.md").exists():
            n += 1
        try:
            import shutil
            name = f"{self._ARCHIVE_PREFIX}.{n}.md"
            shutil.copy2(str(active), str(archive_dir / name))
            try:
                self._note_archive(uid, name,
                                   f"superseded by a new active {self._CAP_NOUN}")
            except Exception:
                logger.debug("%s %s: archive index not updated", self._LOG_LABEL, uid)
        except Exception:
            pass  # archival is best-effort; never block a write on it

    def _note_archive(self, uid: str, archived_name: str, reason: str) -> None:
        """Append one provenance line to ``.archived/INDEX.md`` (035 P2-14).

        A directory of ``self.0.md``/``self.1.md`` says WHAT was archived and
        nothing about WHY or what replaced it — so a superseded rule could not be
        traced back to the decision that superseded it, which is exactly the
        question the 09-08 den incident raised. Append-only, best-effort: the
        caller must never fail a write because provenance could not be recorded.
        """
        index = self._root(uid) / ".archived" / "INDEX.md"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"- {stamp}  {archived_name}  — {reason}\n"
        index.parent.mkdir(parents=True, exist_ok=True)
        header = "" if index.exists() else (
            "# Archived identity documents\n\n"
            "One line per archived version: when, which file, and why.\n\n")
        with open(index, "a", encoding="utf-8") as fh:
            fh.write(header + line)

    def _archive_pending(self, uid: str, pending_f: Path) -> None:
        """Back up a rejected pending draft (best-effort, recoverable rollback)."""
        archive_dir = self._root(uid) / ".archived"
        archive_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        while (archive_dir / f"{self._REJECTED_PREFIX}.{n}.md").exists():
            n += 1
        try:
            import shutil
            name = f"{self._REJECTED_PREFIX}.{n}.md"
            shutil.copy2(str(pending_f), str(archive_dir / name))
            try:
                self._note_archive(uid, name,
                                   f"pending {self._CAP_NOUN} draft rejected or superseded")
            except Exception:
                logger.debug("%s %s: archive index not updated", self._LOG_LABEL, uid)
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
