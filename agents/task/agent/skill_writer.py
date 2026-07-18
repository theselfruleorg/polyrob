"""Writable skills (W2-A, Reference-parity self-authored skills).

POLYROB's :class:`SkillManager` was read-only — the agent could *match* and *load* skills
but never create, refine, or retire them, so it could not learn durable procedures
from experience. This mixin adds the write path, composed into ``SkillManager`` so
all the existing validators/loaders are reused.

Safety is designed in, not bolted on — a self-authored skill is a prompt-injection
**persistence** vector (a skill loaded next session is read as instructions):

1. **Tenant-confined** — writes go ONLY under the user DATA-HOME scope's
   ``user_{user_id}/`` (Task 8: ``skill_store.skills_data_home()``, NOT the
   installed package tree — this is what lets a self-authored skill survive a
   ``polyrob update`` code-swap); the ``^[a-z][a-z0-9-]*$`` id regex
   (``validate_skill_id``) blocks path traversal.
2. **Anon-blocked** — an empty/blank ``user_id`` is refused (mirror
   ``MEMORY_REQUIRE_USER_ID``) so authored skills never land in a shared bucket.
3. **Validated** — ``validate_skill_id`` → ``validate_skill_content``
   (``MAX_SKILL_CONTENT_CHARS``) before any write.
4. **Threat-scanned** — ``threat_scan.is_suspicious`` rejects obviously-injected
   bodies BEFORE persist (unconditional on the write path here — a read-only
   trusted scope, i.e. builtin, is exempt via ``skill_store.scan_exempt``, but
   `create_skill` only ever targets the writable user scope, so this is
   effectively still unconditional; not gated on the memory flag).
5. **Quarantined** — when ``SKILLS_WRITABLE_REQUIRE_REVIEW`` (default true) OR the
   author was a non-user-initiated turn (self-wake / background review), the skill
   lands in ``user_{uid}/.pending/`` **inert** (NOT in rules.json), so a forged turn
   can never auto-activate a skill.
6. **Atomic** — temp-file + ``os.replace`` so a crash never leaves a half-written
   ``SKILL.md``.
7. **Archive-never-delete** — deletes move to ``user_{uid}/.archived/`` (recoverable).

Gated by ``SKILLS_WRITABLE`` at the action layer; the methods here are a library and
do nothing until called.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import List, Optional

from agents.task.agent import skill_store

logger = logging.getLogger(__name__)

PROVENANCE_USER = "user"
PROVENANCE_AGENT = "agent"
PROVENANCE_BACKGROUND = "background_review"
# Authors whose writes must NEVER auto-activate, regardless of REQUIRE_REVIEW: a
# forged (self-wake / background-review) turn. A normal interactive `agent` turn is
# user-initiated, so it follows the REQUIRE_REVIEW flag like any other.
_NON_USER_AUTHORS = frozenset({PROVENANCE_BACKGROUND})


class SkillWriteResult:
    """Outcome of a write op (mirrors SkillValidationResult's shape, plus location)."""

    def __init__(self, skill_id: str, ok: bool, *, errors=None, warnings=None,
                 pending: bool = False, path: Optional[str] = None):
        self.skill_id = skill_id
        self.is_valid = ok
        self.ok = ok
        self.errors = errors or []
        self.warnings = warnings or []
        self.pending = pending
        self.path = path

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
        return self._user_dirs_root() / f"user_{user_id}"

    @staticmethod
    def _require_user(user_id: Optional[str]) -> Optional[str]:
        """Return a clean user_id or None (anon-block). Never write under user_/."""
        if user_id is None:
            return None
        uid = str(user_id).strip()
        return uid or None

    def _resolve_pending(self, created_by: str, pending: Optional[bool],
                         *, overwriting_active: bool = False) -> bool:
        from agents.task.constants import AutonomyConfig
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

    def create_skill(self, skill_id: str, content: str, *, user_id: str,
                     description: str = "", created_by: str = PROVENANCE_AGENT,
                     pending: Optional[bool] = None) -> SkillWriteResult:
        """Author a new user skill (validated, threat-scanned, atomically written)."""
        uid = self._require_user(user_id)
        if uid is None:
            return SkillWriteResult(skill_id, False,
                                    errors=["empty user_id refused (tenant scope)"])

        id_ok, id_errors = self.validate_skill_id(skill_id)
        if not id_ok:
            return SkillWriteResult(skill_id, False, errors=id_errors)

        content_res = self.validate_skill_content(skill_id, content)
        if not content_res.is_valid:
            return SkillWriteResult(skill_id, False, errors=content_res.errors,
                                    warnings=content_res.warnings)

        # Threat-scan is the injection-persistence tripwire. Fail-OPEN only on an
        # import error (scanner absent); if the scan itself RAISES we fail-CLOSED
        # (reject) — a write must not slip past a crashing guard.
        # P1 finalization: scan skill body + description with the composed skill
        # scanner (base injection patterns + invisible/zero-width/bidi unicode) so a
        # hidden instruction set can't be smuggled past the plain-text .pending
        # review — the docs (SKILL_AUTHORING_STANDARD §8) promise the unicode check.
        try:
            from modules.memory.task.threat_scan import (
                is_skill_content_suspicious as is_suspicious,
            )
        except Exception:
            is_suspicious = None
        # Task 8: a read-only + trusted scope (today: builtin) is exempt from a
        # write-time re-scan — future-proofs e.g. a reindex of the shipped
        # library. `create_skill` ALWAYS targets the writable user scope
        # (`skill_store.user_scope()`, writable=True), so `scan_exempt()` is
        # unconditionally False here — this must NEVER weaken scanning on the
        # live (user) write path, regardless of user_scope() also being
        # `trusted` (trusted describes location provenance, not a license to
        # skip scanning newly-written content).
        _write_scope = skill_store.user_scope()
        if is_suspicious is not None and not skill_store.scan_exempt(_write_scope):
            try:
                flagged = is_suspicious(content)
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

        active_file = self._user_root(uid) / skill_id / "SKILL.md"
        overwriting_active = active_file.exists() or (skill_id in getattr(self, "skill_rules", {}))
        quarantine = self._resolve_pending(created_by, pending,
                                           overwriting_active=overwriting_active)
        base = self._user_root(uid) / (".pending" if quarantine else "")
        skill_dir = base / skill_id
        skill_file = skill_dir / "SKILL.md"
        try:
            if skill_file.exists():
                self._archive_prior_body(uid, skill_id, skill_file)  # non-destructive overwrite
            self._atomic_write(skill_file, content)
            if not quarantine:
                self._upsert_rule(uid, skill_id, description=description,
                                  created_by=created_by, content=content)
                self._invalidate_skill_cache(uid, skill_id)
            self._record_provenance(skill_id, uid, created_by)
            logger.info("authored skill %s/%s (%s)", uid, skill_id,
                        "pending" if quarantine else "active")
            return SkillWriteResult(skill_id, True, pending=quarantine,
                                    warnings=content_res.warnings, path=str(skill_file))
        except Exception as e:
            logger.error("skill write failed for %s/%s: %s", uid, skill_id, e, exc_info=True)
            return SkillWriteResult(skill_id, False, errors=[f"write failed: {e}"])

    def patch_skill(self, skill_id: str, *, user_id: str, old_string: str,
                    new_string: str, replace_all: bool = False,
                    created_by: str = PROVENANCE_AGENT,
                    pending: Optional[bool] = None) -> SkillWriteResult:
        """Exact-match edit of an existing user skill body, re-validated + re-scanned."""
        uid = self._require_user(user_id)
        if uid is None:
            return SkillWriteResult(skill_id, False, errors=["empty user_id refused"])
        id_ok, id_errors = self.validate_skill_id(skill_id)
        if not id_ok:
            return SkillWriteResult(skill_id, False, errors=id_errors)

        skill_file = self._find_skill_file(uid, skill_id)
        if skill_file is None:
            return SkillWriteResult(skill_id, False, errors=[f"skill '{skill_id}' not found for user"])
        # A forged (non-user) turn may refine its OWN pending draft but must never
        # mutate an already-ACTIVE skill (that's user-controlled instruction content).
        if created_by in _NON_USER_AUTHORS and not self._is_pending_path(skill_file):
            return SkillWriteResult(skill_id, False,
                                    errors=["a background turn cannot patch an active skill"])
        try:
            current = skill_file.read_text(encoding="utf-8")
        except Exception as e:
            return SkillWriteResult(skill_id, False, errors=[f"read failed: {e}"])

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
                                 pending=pending if pending is not None else self._is_pending_path(skill_file))

    def delete_skill(self, skill_id: str, *, user_id: str,
                     absorbed_into: Optional[str] = None,
                     created_by: str = PROVENANCE_USER) -> bool:
        """Archive a user skill (recoverable). Never hard-deletes; drops its rule."""
        uid = self._require_user(user_id)
        if uid is None:
            return False
        id_ok, _ = self.validate_skill_id(skill_id)
        if not id_ok:
            return False
        skill_file = self._find_skill_file(uid, skill_id)
        if skill_file is None:
            return False
        # A forged (non-user) turn must never retire an ACTIVE skill.
        if created_by in _NON_USER_AUTHORS and not self._is_pending_path(skill_file):
            logger.info("background turn blocked from deleting active skill %s/%s", uid, skill_id)
            return False
        try:
            archive_dir = self._user_root(uid) / ".archived" / skill_id
            archive_dir.mkdir(parents=True, exist_ok=True)
            dest = archive_dir / "SKILL.md"
            os.replace(str(skill_file), str(dest))
            if absorbed_into:
                (archive_dir / "ABSORBED_INTO").write_text(absorbed_into, encoding="utf-8")
            self._remove_rule(uid, skill_id)
            self.reload_rules()
            logger.info("archived skill %s/%s%s", uid, skill_id,
                        f" (absorbed into {absorbed_into})" if absorbed_into else "")
            return True
        except Exception as e:
            logger.error("skill archive failed for %s/%s: %s", uid, skill_id, e, exc_info=True)
            return False

    def promote_pending_skill(self, skill_id: str, *, user_id: str,
                              description: str = "") -> SkillWriteResult:
        """Move a `.pending/` skill into active use (the human/curator review gate)."""
        uid = self._require_user(user_id)
        if uid is None:
            return SkillWriteResult(skill_id, False, errors=["empty user_id refused"])
        id_ok, id_errors = self.validate_skill_id(skill_id)
        if not id_ok:
            return SkillWriteResult(skill_id, False, errors=id_errors)
        pending_file = self._user_root(uid) / ".pending" / skill_id / "SKILL.md"
        if not pending_file.exists():
            return SkillWriteResult(skill_id, False, errors=["no pending skill with that id"])
        content = pending_file.read_text(encoding="utf-8")
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
                                created_by=PROVENANCE_USER, pending=False)
        if res.ok and not res.pending and _orig_author and _orig_author != PROVENANCE_USER:
            try:
                from modules.skills.skill_usage import get_skill_usage_store
                get_skill_usage_store().record_provenance(skill_id, uid, _orig_author)
            except Exception:
                pass
        if res.ok and not res.pending:
            try:
                os.remove(str(pending_file))
            except OSError:
                pass
        return res

    def list_pending_skills(self, user_id: str) -> List[dict]:
        """Enumerate a tenant's `.pending/` skill drafts (owner transparency surface)."""
        uid = self._require_user(user_id)
        if uid is None:
            return []
        pending_root = self._user_root(uid) / ".pending"
        if not pending_root.is_dir():
            return []
        out: List[dict] = []
        for skill_dir in sorted(pending_root.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                body = skill_file.read_text(encoding="utf-8")
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
            archive_dir = self._user_root(uid) / ".archived" / skill_id
            archive_dir.mkdir(parents=True, exist_ok=True)
            os.replace(str(pending_file), str(archive_dir / "SKILL.md"))
            logger.info("rejected pending skill %s/%s (archived)", uid, skill_id)
            return True
        except Exception as e:
            logger.error("skill reject failed for %s/%s: %s", uid, skill_id, e, exc_info=True)
            return False

    # --- internals -----------------------------------------------------------

    def _archive_prior_body(self, uid: str, skill_id: str, current_file: Path) -> None:
        """Copy the body we're about to overwrite into .archived/ (recoverable). Fail-open."""
        try:
            archive_dir = self._user_root(uid) / ".archived" / skill_id
            archive_dir.mkdir(parents=True, exist_ok=True)
            n = 1 + sum(1 for _ in archive_dir.glob("*-SKILL.md"))
            dest = archive_dir / f"{n}-SKILL.md"
            content = current_file.read_text(encoding="utf-8")
            if dest.exists():
                # Collision-safe fallback: use a unique temp name in the same dir.
                fd, _tmp = tempfile.mkstemp(prefix=f"{n}-", suffix="-SKILL.md", dir=str(archive_dir))
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
            else:
                dest.write_text(content, encoding="utf-8")
        except Exception as e:
            logger.debug("archive-on-overwrite skipped for %s/%s: %s", uid, skill_id, e)

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

    def _find_skill_file(self, uid: str, skill_id: str) -> Optional[Path]:
        # Defense-in-depth: never join an unvalidated id into a path (callers also
        # validate, but a bad id must never traverse out of user_{uid}/).
        ok, _ = self.validate_skill_id(skill_id)
        if not ok:
            return None
        for sub in ("", ".pending"):
            f = self._user_root(uid) / sub / skill_id / "SKILL.md" if sub else \
                self._user_root(uid) / skill_id / "SKILL.md"
            if f.exists():
                return f
        return None

    def _is_pending_path(self, path: Path) -> bool:
        return ".pending" in path.parts

    def _user_rules_path(self, uid: str) -> Path:
        return self._user_root(uid) / "rules.json"

    def _load_user_rules_raw(self, uid: str) -> dict:
        p = self._user_rules_path(uid)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _upsert_rule(self, uid: str, skill_id: str, *, description: str, created_by: str,
                     content: str = "") -> None:
        rules = self._load_user_rules_raw(uid)
        entry = rules.get(skill_id, {}) if isinstance(rules.get(skill_id), dict) else {}
        existing_triggers = entry.get("triggers") if isinstance(entry.get("triggers"), dict) else {}
        # An authored skill MUST be match-eligible or it is a dead write: get_skills_for_session
        # skips rules with auto_activate falsey, and only MATCHED skills enter the on-demand
        # catalog/_session_skills that load_skill resolves from. So set auto_activate=True and
        # derive keyword triggers from the id/description so the skill surfaces when relevant.
        entry.update({
            "description": description or entry.get("description", ""),
            "created_by": created_by,
            "auto_activate": True,
            "priority": entry.get("priority", 6),  # below curated system skills (1-5)
            "triggers": existing_triggers or {
                "keywords": self._derive_keywords(skill_id, description, content),
            },
        })
        rules[skill_id] = entry
        self._atomic_write(self._user_rules_path(uid), json.dumps(rules, indent=2))

    @staticmethod
    def _derive_keywords(skill_id: str, description: str, content: str) -> List[str]:
        """Keyword triggers so an authored skill matches relevant future tasks.

        Uses the skill-id tokens + significant words from the description/first heading.
        Conservative (no giant keyword lists) to avoid over-triggering.
        """
        import re as _re
        stop = {"the", "and", "for", "with", "this", "that", "your", "you", "when",
                "how", "use", "using", "a", "an", "to", "of", "in", "on", "is", "it",
                "skill", "do", "doing", "make", "making", "create", "creating",
                # P3-4: too-generic tokens that over-trigger under substring matching.
                "data", "file", "files", "report", "info", "list", "page", "tool"}
        words: List[str] = [t for t in skill_id.split("-") if len(t) > 2 and t not in stop]
        src = f"{description}\n{content[:200]}".lower()
        for tok in _re.findall(r"[a-z][a-z0-9]{3,}", src):
            if tok not in stop and tok not in words:
                words.append(tok)
            if len(words) >= 5:
                break
        return words[:5]

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
            self._atomic_write(self._user_rules_path(uid), json.dumps(rules, indent=2))

    def _record_provenance(self, skill_id: str, uid: str, created_by: str) -> None:
        try:
            from modules.skills.skill_usage import get_skill_usage_store
            get_skill_usage_store().record_provenance(skill_id, uid, created_by)
        except Exception:
            pass  # provenance is a metric, never block a write on it
