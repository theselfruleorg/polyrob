"""Writable skills (W2-A, Reference-parity self-authored skills).

POLYROB's :class:`SkillManager` was read-only — the agent could *match* and *load* skills
but never create, refine, or retire them, so it could not learn durable procedures
from experience. This mixin adds the write path, composed into ``SkillManager`` so
all the existing validators/loaders are reused.

Safety is designed in, not bolted on — a self-authored skill is a prompt-injection
**persistence** vector (a skill loaded next session is read as instructions):

1. **Tenant-confined** — writes go ONLY under the user DATA-HOME scope's
   ``user_{user_id}/`` (Task 8: ``core.skills.scopes.skills_data_home()``, NOT the
   installed package tree — this is what lets a self-authored skill survive a
   ``polyrob update`` code-swap); the ``^[a-z][a-z0-9-]*$`` id regex
   (``validate_skill_id``) blocks path traversal.
2. **Anon-blocked** — an empty/blank ``user_id`` is refused (mirror
   ``MEMORY_REQUIRE_USER_ID``) so authored skills never land in a shared bucket.
3. **Validated** — ``validate_skill_id`` → ``validate_skill_content``
   (``MAX_SKILL_FILE_CHARS``) before any write.
4. **Threat-scanned** — ``threat_scan.is_suspicious`` rejects obviously-injected
   bodies BEFORE persist (unconditional on the write path here — a read-only
   trusted scope, i.e. builtin, is exempt via ``scopes.scan_exempt``, but
   `create_skill` only ever targets the writable user scope, so this is
   effectively still unconditional; not gated on the memory flag).
5. **Quarantined** — when ``SKILLS_WRITABLE_REQUIRE_REVIEW`` (default true) OR the
   author was a non-user-initiated turn (self-wake / background review), the skill
   lands in ``user_{uid}/.pending/`` **inert** (NOT in rules.json), so a forged turn
   can never auto-activate a skill.
6. **Atomic** — temp-file + ``os.replace`` so a crash never leaves a half-written
   ``SKILL.md``.
7. **Archive-never-delete** — deletes and overwrites keep the prior body in
   ``user_{uid}/.archived/<id>/<sha256>-SKILL.md`` (content-addressed, recoverable).
8. **Bundled skills are proposals** (081 WS-5.3): once the bundled library is
   seeded (``modules/skills/seed.py``), a patch/edit/rollback of a bundled id
   lands as a ``.pending/`` draft in the author's tenant; only the owner's
   ``promote`` writes the seeded copy (no deploy). A bundled id is never deleted.
9. **Ledgered** (081 WS-5.2) — every mutation appends one ``skill_provenance_log``
   row (action, actor, before/after sha, archive path, ``absorbed_into``);
   :meth:`rollback_skill` restores an archived revision through the same gate.

Gated by ``SKILLS_WRITABLE`` at the action layer; the methods here are a library and
do nothing until called.
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from core.skills import scopes

logger = logging.getLogger(__name__)

PROVENANCE_USER = "user"
PROVENANCE_AGENT = "agent"
PROVENANCE_BACKGROUND = "background_review"
PROVENANCE_CURATOR = "curator"
# Authors whose writes must NEVER auto-activate, regardless of REQUIRE_REVIEW: a
# forged (self-wake / background-review) turn. A normal interactive `agent` turn is
# user-initiated, so it follows the REQUIRE_REVIEW flag like any other.
_NON_USER_AUTHORS = frozenset({PROVENANCE_BACKGROUND})
#: Authors whose delete is a CONSOLIDATION and must name the skill that absorbed
#: the procedure (review C-8 scope: never a foreground owner/agent delete). The
#: curator's age archive is a usage transition, not a consolidation (``aged``).
_ABSORBER_REQUIRED = frozenset({PROVENANCE_BACKGROUND, PROVENANCE_CURATOR})
#: Keys a pre-081 tenant rules.json row carried that now live in skill_usage.db.
_LEGACY_REVISION_KEYS = ("revision", "content_sha256", "created_by")


class SkillWriteResult:
    """Outcome of a write op (mirrors SkillValidationResult's shape, plus location)."""

    def __init__(self, skill_id: str, ok: bool, *, errors=None, warnings=None,
                 pending: bool = False, path: Optional[str] = None,
                 revision: Optional[str] = None):
        self.skill_id = skill_id
        self.is_valid = ok
        self.ok = ok
        self.errors = errors or []
        self.warnings = warnings or []
        self.pending = pending
        self.path = path
        self.revision = revision

    def __bool__(self) -> bool:
        return bool(self.ok)

    def __repr__(self):
        state = "pending" if self.pending else ("ok" if self.ok else "rejected")
        return f"<SkillWriteResult {self.skill_id} {state} errors={self.errors}>"


class SkillWriterMixin:
    """Create / patch / delete user skills. Composed into SkillManager."""

    def _user_root(self, user_id: str) -> Path:
        # Task 8: routes through SkillManager._user_dirs_root() (data-home by
        # default; the pre-existing single-root test-override contract if
        # `skills_dir` was redirected) rather than always `self.skills_dir` —
        # so a create/patch/delete survives a `polyrob update` code-swap.
        uid = self._require_user(user_id)
        if uid is None:
            raise ValueError("unsafe or empty user_id refused (tenant scope)")
        return self._user_dirs_root() / f"user_{uid}"

    @staticmethod
    def _require_user(user_id: Optional[str]) -> Optional[str]:
        """Reject unsafe identifiers; never sanitize two tenants into one path."""
        from core.instance import is_safe_tenant_id
        if user_id is None:
            return None
        uid = str(user_id).strip()
        return uid if uid and is_safe_tenant_id(uid) else None

    def _read_skill_text(self, path: Path, *, max_bytes: int = 160_000) -> str:
        from core.security.confined_read import read_confined_bytes
        from core.security.confined_write import confined_path
        root = self._user_dirs_root()
        return read_confined_bytes(confined_path(path, root), root.resolve(), max_bytes).decode("utf-8")

    @staticmethod
    def _content_revision(content: str) -> str:
        """Immutable identifier for the exact bytes that were scanned/reviewed."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @contextmanager
    def _skill_write_lock(self, uid: str):
        """Exclusive tenant lock, anchored below the trusted skills root.

        Rules and bodies remain separate legacy files, but all writers use this
        lock. A loader additionally verifies the rule's content hash, so a crash
        between the two writes makes a skill unavailable rather than loading
        mismatched instructions. Platforms without POSIX flock refuse mutation.
        """
        try:
            import fcntl
            from core.security.confined_write import confined_parent
        except ImportError as exc:
            raise OSError("safe skill transaction locking is unavailable") from exc
        lock_path = self._user_root(uid) / ".skill-write.lock"
        with confined_parent(lock_path, self._user_dirs_root(), create=True) as (parent, name):
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OSError("unsafe skill transaction lock")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise OSError("skill transaction is busy; reload and retry") from exc
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)

    def _pending_revision_path(self, uid: str, skill_id: str) -> Path:
        return self._user_root(uid) / ".pending" / skill_id / "REVISION.json"

    def _read_pending_revision(self, uid: str, skill_id: str, content: str) -> str:
        """Read a pending version record; lazily backfill safe legacy drafts."""
        revision = self._content_revision(content)
        path = self._pending_revision_path(uid, skill_id)
        try:
            metadata = json.loads(self._read_skill_text(path, max_bytes=16_384))
            if (isinstance(metadata, dict) and metadata.get("content_sha256") == revision
                    and metadata.get("revision") == revision):
                return revision
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        # A legacy pending body had no review binding. Writing a sidecar does not
        # activate it; promotion will re-scan this exact hash.
        self._atomic_write(path, json.dumps({"revision": revision,
                                             "content_sha256": revision}, indent=2))
        return revision

    def _resolve_pending(self, created_by: str, pending: Optional[bool],
                         *, overwriting_active: bool = False) -> bool:
        from core.config_policy import AutonomyConfig
        # Protect curated ACTIVE skills: a non-owner author (agent or background) may
        # never silently replace one — force a .pending proposal (owner promotes).
        if (overwriting_active and created_by != PROVENANCE_USER
                and AutonomyConfig.skill_overwrite_protect()):
            return True
        if pending is not None:
            base = pending
        else:
            base = AutonomyConfig.skills_writable_require_review()
        # Hard rule: a non-user author can NEVER auto-activate — force quarantine even
        # if review is globally disabled.
        if created_by in _NON_USER_AUTHORS:
            return True
        return base

    def _reserved_id_error(self, skill_id: str) -> Optional[str]:
        """D1: the refusal for a builtin/pack skill id (``SkillManager``
        provides :meth:`reserved_skill_id_error`). Fail-CLOSED: if the reserved
        set cannot be computed, the write is refused rather than risk a shadow."""
        check = getattr(self, "reserved_skill_id_error", None)
        if check is None:
            return None
        try:
            return check(skill_id)
        except Exception as e:
            logger.warning("reserved skill-id check failed for %s: %s", skill_id, e)
            return "reserved skill-id check failed (write refused)"

    def create_skill(self, skill_id: str, content: str, *, user_id: str,
                     description: str = "", created_by: str = PROVENANCE_AGENT,
                     pending: Optional[bool] = None,
                     expected_revision: Optional[str] = None,
                     action: str = "create", prior: Optional[str] = None,
                     bundled: bool = False) -> SkillWriteResult:
        """Author a new user skill (validated, threat-scanned, atomically written).
        ``action`` names the change in the ledger (create/edit/patch/promote/rollback).

        ``prior`` (patch/edit, C-4): the body this write replaces, read under the
        caller's CAS. Then only the lines the write ADDS are threat-scanned, and
        only the lint warnings the prior body did not have are returned.
        ``bundled``: a proposed fix of a seeded bundled skill — always a pending
        draft, the only write a bundled (reserved) id accepts."""
        uid = self._require_user(user_id)
        if uid is None:
            return SkillWriteResult(skill_id, False,
                                    errors=["empty user_id refused (tenant scope)"])

        id_ok, id_errors = self.validate_skill_id(skill_id)
        if not id_ok:
            return SkillWriteResult(skill_id, False, errors=id_errors)
        reserved = None if bundled else self._reserved_id_error(skill_id)
        if reserved:
            return SkillWriteResult(skill_id, False, errors=[reserved])

        content_res = self.validate_skill_content(skill_id, content)
        if not content_res.is_valid:
            return SkillWriteResult(skill_id, False, errors=content_res.errors,
                                    warnings=content_res.warnings)
        warnings = list(content_res.warnings)
        if prior is not None:
            try:
                had = set(self.validate_skill_content(skill_id, prior).warnings)
            except Exception:
                had = set()
            warnings = [w for w in warnings if w not in had]
        scan_text = content if prior is None else self._added_lines(prior, content)

        # An unavailable scanner cannot authorize durable instructions.
        # P1 finalization: scan skill body + description with the composed skill
        # scanner (base injection patterns + invisible/zero-width/bidi unicode) so a
        # hidden instruction set can't be smuggled past the plain-text .pending
        # review — the docs (SKILL_AUTHORING_STANDARD §8) promise the unicode check.
        try:
            from modules.memory.task.threat_scan import (
                is_skill_content_suspicious as is_suspicious,
            )
        except Exception:
            return SkillWriteResult(skill_id, False,
                                    errors=["threat scanner unavailable (write refused)"])
        # Task 8: a read-only + trusted scope (today: builtin) is exempt from a
        # write-time re-scan — future-proofs e.g. a reindex of the shipped
        # library. `create_skill` ALWAYS targets the writable user scope
        # (`scopes.user_scope()`, writable=True), so `scan_exempt()` is
        # unconditionally False here — this must NEVER weaken scanning on the
        # live (user) write path, regardless of user_scope() also being
        # `trusted` (trusted describes location provenance, not a license to
        # skip scanning newly-written content).
        _write_scope = scopes.user_scope()
        if not callable(is_suspicious):
            return SkillWriteResult(skill_id, False,
                                    errors=["threat scanner unavailable (write refused)"])
        if not scopes.scan_exempt(_write_scope):
            try:
                flagged = bool(scan_text) and is_suspicious(scan_text)
            except Exception as e:
                logger.warning("skill write rejected (threat-scan error, fail-closed): %s/%s: %s",
                               uid, skill_id, e)
                return SkillWriteResult(skill_id, False, errors=["threat-scan error (rejected)"])
            if flagged:
                logger.warning("skill write rejected (suspicious content): %s/%s", uid, skill_id)
                return SkillWriteResult(skill_id, False,
                                        errors=["content failed injection threat-scan"])
            # P3-1: the DESCRIPTION is injected verbatim into the <skill-catalog>
            # prompt for every future session — so it is an injection vector exactly
            # like the body and must be scanned too. Fail-CLOSED.
            if description:
                try:
                    desc_flagged = is_suspicious(description)
                except Exception as e:
                    logger.warning("skill write rejected (description scan error, fail-closed): %s/%s: %s",
                                   uid, skill_id, e)
                    return SkillWriteResult(skill_id, False,
                                            errors=["description scan error (rejected)"])
                if desc_flagged:
                    logger.warning("skill write rejected (suspicious description): %s/%s", uid, skill_id)
                    return SkillWriteResult(skill_id, False,
                                            errors=["description failed injection threat-scan"])

        revision = self._content_revision(content)
        try:
            with self._skill_write_lock(uid):
                active_file = self._user_root(uid) / skill_id / "SKILL.md"
                overwriting_active = active_file.exists() or (skill_id in getattr(self, "skill_rules", {}))
                quarantine = bundled or self._resolve_pending(
                    created_by, pending, overwriting_active=overwriting_active)
                base = self._user_root(uid) / (".pending" if quarantine else "")
                skill_dir = base / skill_id
                skill_file = skill_dir / "SKILL.md"
                if expected_revision is not None:
                    try:
                        current = self._read_skill_text(skill_file)
                        actual = self._content_revision(current)
                    except (OSError, UnicodeError):
                        # A protected active skill is patched by writing a fresh
                        # pending draft. Its CAS token names the active body the
                        # editor actually read, not the as-yet-absent draft.
                        try:
                            actual = self._content_revision(self._read_skill_text(active_file)) \
                                if quarantine else None
                        except (OSError, UnicodeError):
                            actual = None
                    if actual != expected_revision:
                        return SkillWriteResult(skill_id, False, revision=actual,
                                                errors=["revision conflict; reload before editing"])
                before_sha, archive_path = None, None
                if skill_file.exists():  # non-destructive overwrite
                    archive_path, before_sha = self._archive_prior_body(uid, skill_id, skill_file)
                # Body first: until the matching rule is committed, hash-verifying
                # loaders refuse this active skill rather than consume mixed state.
                self._atomic_write(skill_file, content)
                if quarantine:
                    self._atomic_write(self._pending_revision_path(uid, skill_id), json.dumps({
                        "revision": revision, "content_sha256": revision,
                        "description": description, "created_by": created_by,
                        **({"bundled": True} if bundled else {}),
                    }, indent=2))
                else:
                    self._upsert_rule(uid, skill_id, description=description)
                    # The commit point: until the revision is recorded, a
                    # hash-verifying loader refuses this body.
                    self._commit_revision(uid, skill_id, revision, created_by)
                self._invalidate_skill_cache(uid, skill_id)
            self._record_provenance(skill_id, uid, created_by)
            self._ledger(skill_id, uid, action=action, actor=created_by, before_sha=before_sha,
                         after_sha=revision, archive_path=archive_path, pending=quarantine)
            logger.info("authored skill %s/%s (%s)", uid, skill_id,
                        "pending" if quarantine else "active")
            return SkillWriteResult(skill_id, True, pending=quarantine,
                                    warnings=warnings, path=str(skill_file),
                                    revision=revision)
        except Exception as e:
            logger.error("skill write failed for %s/%s: %s", uid, skill_id, e, exc_info=True)
            return SkillWriteResult(skill_id, False, errors=[f"write failed: {e}"])

    def patch_skill(self, skill_id: str, *, user_id: str, old_string: str,
                    new_string: str, replace_all: bool = False,
                    created_by: str = PROVENANCE_AGENT,
                    pending: Optional[bool] = None,
                    expected_revision: Optional[str] = None) -> SkillWriteResult:
        """Exact-match edit of an existing user skill body, re-validated + re-scanned."""
        base = self._edit_base(skill_id, user_id, created_by, pending, expected_revision)
        if isinstance(base, SkillWriteResult):
            return base
        uid, current, actual_revision, pending, propose_only, bundled = base
        count = current.count(old_string)
        if count == 0:
            return SkillWriteResult(skill_id, False, errors=["old_string not found"])
        if count > 1 and not replace_all:
            return SkillWriteResult(skill_id, False,
                                    errors=[f"old_string occurs {count}× — pass replace_all=true"])
        updated = (current.replace(old_string, new_string)
                   if replace_all else current.replace(old_string, new_string, 1))
        # Re-run the full validate+scan gate on the NEW body.
        return self.create_skill(skill_id, updated, user_id=uid, created_by=created_by,
                                 pending=pending,
                                 expected_revision=None if propose_only else actual_revision,
                                 action="patch", prior=current, bundled=bundled)

    def edit_skill(self, skill_id: str, content: str, *, user_id: str, description: str = "",
                   created_by: str = PROVENANCE_AGENT, pending: Optional[bool] = None,
                   expected_revision: Optional[str] = None,
                   keep_frontmatter=None) -> SkillWriteResult:
        """Replace an existing user skill's whole body (the same base, CAS and
        quarantine as :meth:`patch_skill`). ``keep_frontmatter(current, content)``
        may carry the current frontmatter into a body that has none."""
        base = self._edit_base(skill_id, user_id, created_by, pending, expected_revision)
        if isinstance(base, SkillWriteResult):
            return base
        uid, current, actual_revision, pending, propose_only, bundled = base
        if callable(keep_frontmatter):
            content = keep_frontmatter(current, content)
        return self.create_skill(skill_id, content, user_id=uid, description=description,
                                 created_by=created_by, pending=pending,
                                 expected_revision=None if propose_only else actual_revision,
                                 action="edit", prior=current, bundled=bundled)

    def _edit_base(self, skill_id: str, user_id: str, created_by: str,
                   pending: Optional[bool], expected_revision: Optional[str]):
        """``(uid, current, sha, pending, propose_only, bundled)`` for a patch/edit,
        or the refusal. The base is the file the write lands in
        (:meth:`_patch_base_file`; for a bundled id its draft, else the seeded copy);
        a stale ``expected_revision`` refuses with the current sha."""
        uid = self._require_user(user_id)
        if uid is None:
            return SkillWriteResult(skill_id, False, errors=["empty user_id refused"])
        id_ok, id_errors = self.validate_skill_id(skill_id)
        if not id_ok:
            return SkillWriteResult(skill_id, False, errors=id_errors)
        bundled = skill_id in self._bundled_ids()
        skill_file = (self._bundled_base_file(uid, skill_id) if bundled else
                      self._patch_base_file(uid, skill_id, created_by=created_by, pending=pending))
        if skill_file is None:
            return SkillWriteResult(skill_id, False, errors=[f"skill '{skill_id}' not found for user"])
        # A forged (non-user) turn may refine its OWN pending draft but must never
        # mutate an already-ACTIVE skill: 056 WS6 lands a pending REVISION of it under
        # `.pending/<id>/` for the owner's `promote` (the active file is never touched).
        propose_only = bundled or (created_by in _NON_USER_AUTHORS
                                   and not self._is_pending_path(skill_file))
        if propose_only:
            pending = True
        try:
            current = self._read_skill_text(skill_file)
        except Exception as e:
            return SkillWriteResult(skill_id, False, errors=[f"read failed: {e}"])
        actual = self._content_revision(current)
        if expected_revision is not None and expected_revision != actual:
            where = (" (the edit applies to the pending draft of this skill)"
                     if self._is_pending_path(skill_file) else "")
            return SkillWriteResult(skill_id, False, revision=actual,
                                    errors=[f"revision conflict; reload before editing{where}"])
        if pending is None:
            pending = self._is_pending_path(skill_file)
        return uid, current, actual, pending, propose_only, bundled

    @staticmethod
    def _added_lines(prior: str, content: str) -> str:
        """The lines ``content`` adds to ``prior`` (inserted or replaced), joined."""
        import difflib
        old, new = prior.splitlines(), content.splitlines()
        out: List[str] = []
        for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new,
                                                             autojunk=False).get_opcodes():
            if tag in ("insert", "replace"):
                out.extend(new[j1:j2])
        return "\n".join(out)

    def delete_skill(self, skill_id: str, *, user_id: str,
                     absorbed_into: Optional[str] = None,
                     created_by: str = PROVENANCE_USER, aged: bool = False) -> bool:
        """Archive a user skill (recoverable); ``True`` when it moved. See
        :meth:`archive_skill` for the refusal text."""
        return bool(self.archive_skill(skill_id, user_id=user_id, absorbed_into=absorbed_into,
                                       created_by=created_by, aged=aged))

    def archive_skill(self, skill_id: str, *, user_id: str,
                      absorbed_into: Optional[str] = None,
                      created_by: str = PROVENANCE_USER, aged: bool = False,
                      expected_revision: Optional[str] = None) -> SkillWriteResult:
        """Archive a user skill (recoverable). Never hard-deletes; drops its rule.

        A background-review or curator delete is a consolidation and must name
        ``absorbed_into`` (an existing, different skill); the curator's age
        archive passes ``aged=True`` instead. ``expected_revision`` is the sha256
        of the body the caller read; a mismatch refuses with the current one."""
        uid = self._require_user(user_id)
        if uid is None:
            return SkillWriteResult(skill_id, False, errors=["empty user_id refused"])
        id_ok, id_errors = self.validate_skill_id(skill_id)
        if not id_ok:
            return SkillWriteResult(skill_id, False, errors=id_errors)
        skill_file = self._find_skill_file(uid, skill_id)
        if skill_file is None:
            if skill_id in self._bundled_ids():
                return SkillWriteResult(skill_id, False, errors=[
                    f"`{skill_id}` is a bundled skill: it is not deleted — patch it (the "
                    f"owner accepts the fix)"])
            return SkillWriteResult(skill_id, False, errors=[f"no skill `{skill_id}` to archive"])
        # A forged (non-user) turn must never retire an ACTIVE skill.
        if created_by in _NON_USER_AUTHORS and not self._is_pending_path(skill_file):
            logger.info("background turn blocked from deleting active skill %s/%s", uid, skill_id)
            return SkillWriteResult(skill_id, False, errors=[
                "a background turn cannot delete an active skill"])
        absorber_error = self._absorber_error(uid, skill_id, absorbed_into, created_by,
                                              aged=aged and created_by == PROVENANCE_CURATOR)
        if absorber_error:
            return SkillWriteResult(skill_id, False, errors=[absorber_error])
        try:
            with self._skill_write_lock(uid):
                current = self._read_skill_text(skill_file)
                before = self._content_revision(current)
                if expected_revision is not None and expected_revision != before:
                    return SkillWriteResult(skill_id, False, revision=before,
                                            errors=["revision conflict; reload before editing"])
                archive_path, _ = self._archive_prior_body(uid, skill_id, skill_file)
                from core.security.confined_write import unlink_confined
                unlink_confined(skill_file, self._user_dirs_root())
                self._remove_rule(uid, skill_id)
                try:
                    self._commit_revision(uid, skill_id, None, created_by)
                except Exception:
                    logger.warning("revision clear failed for archived skill %s/%s", uid, skill_id)
            self._ledger(skill_id, uid, action="archive" if aged else "delete", actor=created_by,
                         before_sha=before, archive_path=archive_path,
                         absorbed_into=absorbed_into or None,
                         pending=self._is_pending_path(skill_file))
            self.reload_rules()
            logger.info("archived skill %s/%s%s", uid, skill_id,
                        f" (absorbed into {absorbed_into})" if absorbed_into else "")
            return SkillWriteResult(skill_id, True, path=archive_path, revision=before)
        except Exception as e:
            logger.error("skill archive failed for %s/%s: %s", uid, skill_id, e, exc_info=True)
            return SkillWriteResult(skill_id, False, errors=[f"archive failed: {e}"])

    def _absorber_error(self, uid: str, skill_id: str, absorbed_into: Optional[str],
                        created_by: str, *, aged: bool) -> Optional[str]:
        """The C-8 rule: a consolidation delete names a live, different absorber."""
        if not absorbed_into:
            if created_by in _ABSORBER_REQUIRED and not aged:
                return ("a background or curator delete must name absorbed_into "
                        "(the skill that now holds this procedure)")
            return None
        if absorbed_into == skill_id or not self.validate_skill_id(absorbed_into)[0]:
            return f"absorbed_into '{absorbed_into}' must be another valid skill id"
        resolve = getattr(self, "resolve_skill_dir", None)
        try:
            found = resolve(absorbed_into, uid) if callable(resolve) else \
                self._find_skill_file(uid, absorbed_into)
        except Exception:
            found = None
        if found is None:
            return f"absorbed_into '{absorbed_into}' is not a skill this tenant has"
        return None

    def rollback_skill(self, skill_id: str, *, user_id: str, to_sha: Optional[str] = None,
                       created_by: str = PROVENANCE_AGENT) -> SkillWriteResult:
        """Restore an archived revision of a user skill through the normal write
        gate (validated, scanned, quarantined for a non-owner author).

        ``to_sha`` (full sha256 or a unique prefix of 8+ chars) names the revision;
        without it, the revision the newest active change replaced. Fail-closed:
        an unreadable ledger or a missing archived body refuses."""
        uid = self._require_user(user_id)
        if uid is None:
            return SkillWriteResult(skill_id, False, errors=["empty user_id refused"])
        id_ok, id_errors = self.validate_skill_id(skill_id)
        if not id_ok:
            return SkillWriteResult(skill_id, False, errors=id_errors)
        bundled = skill_id in self._bundled_ids()
        archived = self._archived_bodies(uid, skill_id, seed_root=self._seed_root() if bundled else None)
        if to_sha is None:
            try:
                from modules.skills.skill_usage import get_skill_usage_store
                rows = (get_skill_usage_store().list_changes(skill_id, scope="bundled")
                        if bundled else get_skill_usage_store().list_changes(skill_id, uid))
            except Exception as e:
                return SkillWriteResult(skill_id, False, errors=[f"skill ledger unreadable: {e}"])
            to_sha = next((r["before_sha"] for r in rows
                           if r.get("before_sha") and not r.get("pending")), None)
            if to_sha is None:
                return SkillWriteResult(skill_id, False,
                                        errors=["no earlier active revision in the ledger"])
        want = (to_sha or "").strip().lower()
        hits = [sha for sha in archived if sha == want or (len(want) >= 8 and sha.startswith(want))]
        if len(hits) != 1:
            known = ", ".join(sorted(sha[:12] for sha in archived)) or "none"
            why = "is ambiguous" if hits else "is not archived"
            return SkillWriteResult(skill_id, False,
                                    errors=[f"revision {want[:12] or '?'} {why} (archived: {known})"])
        description = ""
        rule = self._load_user_rules_raw(uid).get(skill_id)
        if isinstance(rule, dict):
            description = str(rule.get("description") or "")
        return self.create_skill(skill_id, archived[hits[0]], user_id=uid,
                                 description=description, created_by=created_by,
                                 action="rollback", bundled=bundled)

    def _archived_bodies(self, uid: str, skill_id: str, *, seed_root: Optional[Path] = None) -> dict:
        """``{sha256: body}`` of every archived body of ``skill_id`` (any name:
        the content-addressed ``<sha>-SKILL.md``, a legacy ``<uuid>-SKILL.md`` or
        ``SKILL.md``) — the tenant's archive, or a bundled skill's in ``seed_root``.
        Unreadable entries are skipped."""
        out: dict = {}
        archive_dir = (seed_root if seed_root is not None else self._user_root(uid)) \
            / ".archived" / skill_id
        try:
            from core.security.confined_write import list_confined_directory
            names = list_confined_directory(archive_dir, self._user_dirs_root())
        except OSError:
            return out
        for name in names:
            if not name.endswith(".md"):
                continue
            try:
                body = self._read_skill_text(archive_dir / name)
            except (OSError, UnicodeError):
                continue
            out[self._content_revision(body)] = body
        return out

    def promote_pending_skill(self, skill_id: str, *, user_id: str,
                              description: str = "",
                              expected_revision: Optional[str] = None) -> SkillWriteResult:
        """Move a `.pending/` skill into active use (the human/curator review gate)."""
        uid = self._require_user(user_id)
        if uid is None:
            return SkillWriteResult(skill_id, False, errors=["empty user_id refused"])
        id_ok, id_errors = self.validate_skill_id(skill_id)
        if not id_ok:
            return SkillWriteResult(skill_id, False, errors=id_errors)
        if skill_id in self._bundled_ids():
            return self._promote_bundled(uid, skill_id, expected_revision)
        # D1: a draft staged under a builtin/pack id (before the writer refused
        # one) must never activate — promote would replace the builtin's body,
        # triggers, `requires` and gate.
        reserved = self._reserved_id_error(skill_id)
        if reserved:
            return SkillWriteResult(skill_id, False, errors=[reserved])
        pending_file = self._user_root(uid) / ".pending" / skill_id / "SKILL.md"
        if not pending_file.exists():
            return SkillWriteResult(skill_id, False, errors=["no pending skill with that id"])
        try:
            content = self._read_skill_text(pending_file)
        except (OSError, UnicodeError) as exc:
            return SkillWriteResult(skill_id, False, errors=[f"unsafe pending skill: {exc}"])
        try:
            with self._skill_write_lock(uid):
                # This sidecar is the review token. Re-read while locked so a
                # patch after review cannot be promoted under an old approval.
                content = self._read_skill_text(pending_file)
                reviewed_revision = self._read_pending_revision(uid, skill_id, content)
                if expected_revision is not None and expected_revision != reviewed_revision:
                    return SkillWriteResult(skill_id, False,
                                            errors=["revision conflict; pending skill changed after review"])
        except OSError as exc:
            return SkillWriteResult(skill_id, False, errors=[f"promotion lock failed: {exc}"])
        # P2-20: read the ORIGINAL author before we re-create. Promote must ACTIVATE the
        # skill (that's the review gate), so we still create it as PROVENANCE_USER —
        # passing the original author (e.g. background_review) would make create_skill's
        # quarantine logic re-quarantine it and promote would never activate. Instead we
        # re-record the original authorship in provenance AFTER activation, so a promoted
        # skill stays ACTIVE yet remains in the curator's authored scope AND the
        # authored-reuse metric (where a successfully promoted agent skill should count).
        _orig_author = None
        try:
            from modules.skills.skill_usage import get_skill_usage_store
            _prior = get_skill_usage_store().get_provenance(skill_id, uid)
            if _prior and _prior.get("created_by"):
                _orig_author = _prior["created_by"]
        except Exception:
            _orig_author = None
        res = self.create_skill(skill_id, content, user_id=uid, description=description,
                                created_by=PROVENANCE_USER, pending=False, action="promote")
        if res.ok and not res.pending and _orig_author and _orig_author != PROVENANCE_USER:
            try:
                from modules.skills.skill_usage import get_skill_usage_store
                get_skill_usage_store().record_provenance(skill_id, uid, _orig_author)
            except Exception:
                pass
        if res.ok and not res.pending:
            try:
                from core.security.confined_write import unlink_confined
                # Delete only the exact version that was promoted. A newer draft
                # left by a concurrent editor remains pending for a fresh review.
                with self._skill_write_lock(uid):
                    latest = self._read_skill_text(pending_file)
                    if self._content_revision(latest) != reviewed_revision:
                        return res
                    unlink_confined(pending_file, self._user_dirs_root())
                    try:
                        unlink_confined(self._pending_revision_path(uid, skill_id), self._user_dirs_root())
                    except OSError:
                        pass
                unlink_confined(pending_file.parent, self._user_dirs_root(), directory=True)
            except OSError:
                pass
        return res

    def list_pending_skills(self, user_id: str) -> List[dict]:
        """Enumerate a tenant's `.pending/` skill drafts (owner transparency surface)."""
        uid = self._require_user(user_id)
        if uid is None:
            return []
        pending_root = self._user_root(uid) / ".pending"
        try:
            from core.security.confined_write import list_confined_directory
            entries = list_confined_directory(pending_root, self._user_dirs_root())
        except OSError:
            return []
        out: List[dict] = []
        for entry in entries:
            if not self.validate_skill_id(entry)[0]:
                continue
            skill_dir = pending_root / entry
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                body = self._read_skill_text(skill_file)
            except Exception:
                continue
            preview = body.strip().replace("\n", " ")
            if len(preview) > 280:
                preview = preview[:277] + "…"
            out.append({
                "kind": "skill",
                "skill_id": skill_dir.name,
                "user_id": uid,
                "preview": preview,
                "chars": len(body),
                "path": str(skill_file),
                "revision": self._content_revision(body),
                "bundled": skill_dir.name in self._bundled_ids(),
            })
        return out

    def reject_pending_skill(self, skill_id: str, *, user_id: str) -> bool:
        """Discard a `.pending/` skill draft (owner rejects). Archive-never-delete;
        never touches an active skill (a non-pending id is refused)."""
        uid = self._require_user(user_id)
        if uid is None:
            return False
        id_ok, _ = self.validate_skill_id(skill_id)
        if not id_ok:
            return False
        pending_file = self._user_root(uid) / ".pending" / skill_id / "SKILL.md"
        if not pending_file.exists():
            return False
        try:
            from core.security.confined_write import replace_confined, unlink_confined
            body = self._read_skill_text(pending_file)
            sha = self._content_revision(body)
            dest = self._user_root(uid) / ".archived" / skill_id / f"{sha}-SKILL.md"
            replace_confined(pending_file, dest, self._user_dirs_root())
            self._ledger(skill_id, uid, action="reject", actor=PROVENANCE_USER,
                         before_sha=sha, archive_path=str(dest), pending=True)
            try:
                unlink_confined(self._pending_revision_path(uid, skill_id), self._user_dirs_root())
            except OSError:
                pass
            try:
                unlink_confined(pending_file.parent, self._user_dirs_root(), directory=True)
            except OSError:
                pass
            logger.info("rejected pending skill %s/%s (archived)", uid, skill_id)
            return True
        except Exception as e:
            logger.error("skill reject failed for %s/%s: %s", uid, skill_id, e, exc_info=True)
            return False

    # --- internals -----------------------------------------------------------

    def _archive_prior_body(self, uid: str, skill_id: str, current_file: Path) -> tuple:
        """Preserve prior bytes before an overwrite or a delete, content-addressed
        (``<sha256>-SKILL.md``: one body is stored once). Returns ``(path, sha)``;
        an unsafe archive raises, which refuses the write."""
        content = self._read_skill_text(current_file)
        sha = self._content_revision(content)
        dest = self._user_root(uid) / ".archived" / skill_id / f"{sha}-SKILL.md"
        if not dest.exists():
            self._atomic_write(dest, content)
        return str(dest), sha

    # --- the seeded bundled library (081 WS-5.3) ----------------------------------

    def _seed_root(self) -> Optional[Path]:
        """The seeded bundled root this manager serves as builtin, or ``None``."""
        root = Path(getattr(self, "skills_dir", "") or ".")
        return root if (root / scopes.SEED_MANIFEST).is_file() else None

    def _bundled_ids(self) -> set:
        root = self._seed_root()
        if root is None:
            return set()
        try:
            return set(scopes.read_seed_manifest(root))
        except Exception:
            logger.warning("seed manifest unreadable; bundled skills are read-only", exc_info=True)
            return set()

    def _bundled_base_file(self, uid: str, skill_id: str) -> Optional[Path]:
        """The base a fix of a bundled skill edits: the author's pending draft if
        one exists (one draft, one owner accept), else the seeded copy."""
        for f in (self._user_root(uid) / ".pending" / skill_id / "SKILL.md",
                  self._seed_root() / skill_id / "SKILL.md"):
            try:
                self._read_skill_text(f)
                return f
            except (OSError, UnicodeError, ValueError):
                continue
        return None

    def _promote_bundled(self, uid: str, skill_id: str,
                         expected_revision: Optional[str]) -> SkillWriteResult:
        """The owner accepts a fix of a bundled skill: the reviewed draft becomes
        the seeded copy (prior body archived; the next seed keeps it as the
        owner's). Validated, and the lines it adds are scanned, fail-closed."""
        from modules.skills.seed import archive_body, seed_lock
        root = self._seed_root()
        pending_file = self._user_root(uid) / ".pending" / skill_id / "SKILL.md"
        target = root / skill_id / "SKILL.md"
        try:
            with self._skill_write_lock(uid):
                content = self._read_skill_text(pending_file)
                reviewed = self._read_pending_revision(uid, skill_id, content)
                if expected_revision is not None and expected_revision != reviewed:
                    return SkillWriteResult(skill_id, False, errors=[
                        "revision conflict; pending skill changed after review"])
                prior = self._read_skill_text(target)
        except (OSError, UnicodeError) as exc:
            return SkillWriteResult(skill_id, False, errors=[f"no pending fix to accept: {exc}"])
        check = self.validate_skill_content(skill_id, content)
        if not check.is_valid:
            return SkillWriteResult(skill_id, False, errors=check.errors)
        try:
            from modules.memory.task.threat_scan import is_skill_content_suspicious
            added = self._added_lines(prior, content)
            if added and is_skill_content_suspicious(added):
                return SkillWriteResult(skill_id, False,
                                        errors=["content failed injection threat-scan"])
        except Exception as exc:
            return SkillWriteResult(skill_id, False, errors=[f"threat-scan error (rejected): {exc}"])
        before = self._content_revision(prior)
        try:
            with seed_lock(root):
                path = archive_body(root, skill_id, prior)
                archive_body(root, skill_id, content)  # the accepted blob: a restore source
                # The promote row is what makes the next seed KEEP this copy
                # (seed.accepted_sha): write it first and refuse the accept when
                # it cannot land, or the next seed reverts the owner's fix.
                try:
                    from modules.skills.skill_usage import get_skill_usage_store
                    get_skill_usage_store().record_change(
                        skill_id, uid, action="promote", actor=PROVENANCE_USER,
                        before_sha=before, after_sha=reviewed, archive_path=path,
                        scope="bundled")
                except Exception as exc:
                    logger.error("bundled promote ledger failed for %s: %s", skill_id, exc,
                                 exc_info=True)
                    return SkillWriteResult(skill_id, False, errors=[
                        f"ledger write failed ({type(exc).__name__}); fix not applied — "
                        "accept it again"])
                self._atomic_write(target, content)
            from core.security.confined_write import unlink_confined
            # Delete only the exact draft that was accepted: a newer one left by a
            # concurrent patch stays pending for a fresh review.
            try:
                with self._skill_write_lock(uid):
                    latest = self._content_revision(self._read_skill_text(pending_file))
                    if latest == reviewed:
                        for f in (pending_file, self._pending_revision_path(uid, skill_id)):
                            try:
                                unlink_confined(f, self._user_dirs_root())
                            except OSError:
                                pass
            except (OSError, UnicodeError):
                pass  # the fix is written; a draft left behind stays visibly pending
            try:
                unlink_confined(pending_file.parent, self._user_dirs_root(), directory=True)
            except OSError:
                pass
        except Exception as exc:
            logger.error("bundled promote failed for %s: %s", skill_id, exc, exc_info=True)
            return SkillWriteResult(skill_id, False, errors=[f"write failed: {exc}"])
        for cache_name in ("skill_cache", "skill_meta_cache"):
            cache = getattr(self, cache_name, None)
            if isinstance(cache, dict):
                for key in [k for k in cache if k == skill_id or k.endswith(f":{skill_id}")]:
                    cache.pop(key, None)
        reload = getattr(self, "reload_rules", None)
        if callable(reload):
            reload()
        logger.info("owner accepted a fix of bundled skill %s", skill_id)
        return SkillWriteResult(skill_id, True, path=str(target), revision=reviewed)

    def _ledger(self, skill_id: str, uid: str, **row) -> None:
        """Append the change to ``skill_provenance_log``. The write already
        landed: a ledger failure is logged, never raised."""
        try:
            from modules.skills.skill_usage import get_skill_usage_store
            get_skill_usage_store().record_change(skill_id, uid, **row)
        except Exception:
            logger.warning("skill ledger write failed for %s/%s", uid, skill_id, exc_info=True)

    def _atomic_write(self, path: Path, content: str) -> None:
        from core.security.confined_write import write_confined_text
        write_confined_text(path, self._user_dirs_root(), content)

    def _patch_base_file(self, uid: str, skill_id: str, *, created_by: str,
                         pending: Optional[bool]) -> Optional[Path]:
        """The body :meth:`patch_skill` must edit — the file the write will LAND in.

        ⚠️ This is the whole fix for the 2026-09-21 double-approval loop.
        :meth:`_find_skill_file` prefers the ACTIVE ``SKILL.md``, but a non-owner
        patch of an active skill is quarantined by :meth:`_resolve_pending` and
        LANDS in ``.pending/<id>/SKILL.md``. So ``patch_skill`` read one file,
        derived its CAS token from it, and handed that token to ``create_skill``,
        which compared it against a DIFFERENT file. Once a draft existed, every
        later patch failed ``revision conflict`` with no reachable remedy, and
        each edit had to be approved on its own before the next could start.
        Worse, had the CAS passed, the edit would have been computed from the
        active body and would have overwritten (silently discarded) the draft.

        So: when the write will be quarantined and a draft already exists, the
        draft IS the base. A chain of edits then accumulates into ONE pending
        item and costs the owner ONE approval. The active file is still never
        written by a non-owner author — the quarantine rule is untouched.
        """
        active_file = self._user_root(uid) / skill_id / "SKILL.md"
        overwriting_active = (active_file.exists()
                              or (skill_id in getattr(self, "skill_rules", {})))
        try:
            quarantine = self._resolve_pending(created_by, pending,
                                               overwriting_active=overwriting_active)
        except Exception:  # fail-open to the legacy resolution order
            quarantine = False
        if quarantine:
            draft = self._user_root(uid) / ".pending" / skill_id / "SKILL.md"
            try:
                self._read_skill_text(draft)
                return draft
            except (OSError, UnicodeError):
                pass
        return self._find_skill_file(uid, skill_id)

    def _find_skill_file(self, uid: str, skill_id: str) -> Optional[Path]:
        # Defense-in-depth: never join an unvalidated id into a path (callers also
        # validate, but a bad id must never traverse out of user_{uid}/).
        ok, _ = self.validate_skill_id(skill_id)
        if not ok or self._require_user(uid) is None:
            return None
        for sub in ("", ".pending"):
            f = self._user_root(uid) / sub / skill_id / "SKILL.md" if sub else \
                self._user_root(uid) / skill_id / "SKILL.md"
            try:
                self._read_skill_text(f)
                return f
            except (OSError, UnicodeError):
                continue
        return None

    def _is_pending_path(self, path: Path) -> bool:
        return ".pending" in path.parts

    def _user_rules_path(self, uid: str) -> Path:
        return self._user_root(uid) / "rules.json"

    def _save_user_rules(self, uid: str, rules: dict) -> None:
        """The ONE writer of a tenant ``rules.json`` (atomic, confined); the
        skill writer, the loader and the REST skill API all go through it."""
        self._atomic_write(self._user_rules_path(uid), json.dumps(rules, indent=2))

    def _load_user_rules_raw(self, uid: str) -> dict:
        """The ONE parser of a tenant ``rules.json``. Raises on a corrupt file (the
        revision check must not treat it as empty); ``SkillManager._load_user_rules``
        is the tolerant view for loading."""
        p = self._user_rules_path(uid)
        if p.exists():
            try:
                return json.loads(self._read_skill_text(p, max_bytes=1_048_576))
            except FileNotFoundError:
                return {}
        return {}

    def _expected_content_hash(self, user_id: str, skill_id: str) -> Optional[str]:
        """The reviewed ``content_sha256`` of an active tenant skill, or ``None``
        for a legacy (unversioned) one. RAISES when the revision metadata cannot
        be read or is malformed: a versioned skill must never load unverified.

        081 WS-4.2: the revision lives in ``skill_usage.db`` (``skill_provenance``).
        Read-old: a skill written before that keeps its hash in the tenant
        ``rules.json`` until its next write moves it.
        """
        import re as _re
        uid = self._require_user(user_id)
        if uid is None:
            raise ValueError("unsafe tenant id")
        from modules.skills.skill_usage import get_skill_usage_store
        expected = get_skill_usage_store().get_revision(skill_id, uid)
        if expected is None:
            rules = self._load_user_rules_raw(uid)
            if not isinstance(rules, dict):
                raise ValueError("user rules must be an object")
            rule = rules.get(skill_id, {})
            expected = rule.get("content_sha256") if isinstance(rule, dict) else None
        if expected is not None and (not isinstance(expected, str)
                                     or not _re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValueError("malformed revision metadata")
        return expected

    def _commit_revision(self, uid: str, skill_id: str, revision: Optional[str],
                         created_by: str) -> None:
        """Record (or clear, with ``None``) an active skill's revision in the db,
        then drop the legacy copies from the tenant ``rules.json`` (write-new).
        The db write raises; the legacy clean-up is best-effort (the db row wins)."""
        from modules.skills.skill_usage import get_skill_usage_store
        get_skill_usage_store().record_revision(skill_id, uid, revision, created_by)
        try:
            rules = self._load_user_rules_raw(uid)
            entry = rules.get(skill_id) if isinstance(rules, dict) else None
            if isinstance(entry, dict) and any(k in entry for k in _LEGACY_REVISION_KEYS):
                for k in _LEGACY_REVISION_KEYS:
                    entry.pop(k, None)
                self._save_user_rules(uid, rules)
        except Exception:
            logger.debug("legacy revision clean-up skipped for %s/%s", uid, skill_id, exc_info=True)

    def _upsert_rule(self, uid: str, skill_id: str, *, description: str) -> None:
        """The tenant rule: description, priority and ``auto_activate`` (an index
        line; a skill without it is a dead write no session can see). The index
        line itself is the SKILL.md ``metadata.polyrob-when``; the revision and the
        author are in ``skill_usage.db`` (:meth:`_commit_revision`)."""
        rules = self._load_user_rules_raw(uid)
        entry = rules.get(skill_id, {}) if isinstance(rules.get(skill_id), dict) else {}
        entry.update({
            "description": description or entry.get("description", ""),
            "auto_activate": True,
            "priority": entry.get("priority", 6),  # below curated system skills (1-5)
        })
        # 081 Q2: no keyword matcher reads triggers; a tenant skill has no gate.
        entry.pop("triggers", None)
        entry.pop("created_by", None)  # provenance lives in skill_usage.db
        rules[skill_id] = entry
        self._save_user_rules(uid, rules)

    def _invalidate_skill_cache(self, uid: str, skill_id: str) -> None:
        cache = getattr(self, "skill_cache", None)
        if isinstance(cache, dict):
            cache.pop(f"{uid}:{skill_id}", None)
            cache.pop(skill_id, None)
        # Sibling of skill_cache (Task 5): the parsed-frontmatter cache must be
        # invalidated in lockstep, else a stale meta['description'] could survive
        # an overwrite and keep surfacing in the catalog.
        meta_cache = getattr(self, "skill_meta_cache", None)
        if isinstance(meta_cache, dict):
            meta_cache.pop(f"{uid}:{skill_id}", None)
            meta_cache.pop(skill_id, None)

    def _remove_rule(self, uid: str, skill_id: str) -> None:
        rules = self._load_user_rules_raw(uid)
        if skill_id in rules:
            rules.pop(skill_id, None)
            self._save_user_rules(uid, rules)

    def _record_provenance(self, skill_id: str, uid: str, created_by: str) -> None:
        try:
            from modules.skills.skill_usage import get_skill_usage_store
            get_skill_usage_store().record_provenance(skill_id, uid, created_by)
        except Exception:
            pass  # provenance is a metric, never block a write on it
