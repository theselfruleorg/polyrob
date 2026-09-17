"""Tenant-scoped worker (agent-profile) store — 043 §6, WK1.

Completes and HARDENS the pre-existing profile mechanism
(``agents/task/agent/profile_registry.py`` + ``delegate_task(profile=…)``). A
"worker" is exactly an :class:`~agents.task.config.AgentProfileModel` — a named,
reusable agent configuration (prompt / llm / tools / limits). The legacy
``profile_registry`` loads a SINGLE global, un-tenant-scoped dir
(``<project_root>/data/task/profiles``) inside the installed code tree; this
module is the completed store:

1. **Under the data home, TENANT-SCOPED** — approved workers live at
   ``<data_home>/profiles/user_{uid}/`` with a ``.pending/`` quarantine and an
   ``.archived/`` lane BESIDE it (db_manifest-style path SSOT:
   :func:`profiles_data_home`). Data-home (not the code tree) survives a
   ``polyrob update`` code-swap, and the ``user_{uid}/`` scope means no
   cross-tenant leak. Mirrors ``agents/task/agent/skill_store.py``'s data-home
   resolution and ``core/self_context_writer.py``'s ``user_{uid}/`` +
   ``.pending/`` + ``.archived/`` layout.
2. **Scanned body AND description SEPARATELY, fail-CLOSED** — a worker's
   description is injected verbatim into a ``<worker-catalog>`` foundation
   message (043 §6 Discovery) for every future session, so it is an
   injection-persistence vector "exactly like the body" (mirrors
   ``skill_writer.py:152-177``). BOTH are threat-scanned; a flagged body OR
   description, a RAISING scan, OR an unavailable scanner all force the write
   into ``.pending/`` (quarantine, not publish) — the mitigation is quarantine,
   not framing (``untrusted_wrap`` cannot reach a schema field).
3. **Forged / delegated / leaf author → ``.pending/``, UNDISPATCHABLE** — a
   non-user author (``PROVENANCE_BACKGROUND``) can NEVER auto-activate a worker;
   its write is quarantined and :meth:`ProfileStore.get_approved` refuses a
   pending profile, so it can never be dispatched. Origin detection is the
   caller's job (it passes ``created_by``), exactly as ``skill_writer`` does.
4. **Atomic** — temp-file + ``os.replace`` (a crash never leaves a half-written
   profile). **Archive-never-delete** — an overwritten/removed profile is moved
   to ``.archived/`` (recoverable).

Gated by ``WORKERS_ENABLED`` (default OFF). With it OFF the delegation path never
consults this store, so ``delegate_task(profile=…)`` is byte-identical to today.
This module is a library; nothing runs until the flag is on and a worker exists.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Union

from agents.task.config import AgentProfileModel

logger = logging.getLogger(__name__)

PROVENANCE_USER = "user"
PROVENANCE_AGENT = "agent"
PROVENANCE_BACKGROUND = "background_review"
# Authors whose writes must NEVER auto-activate, regardless of any review flag: a
# forged (self-wake / background-review) or delegated/leaf turn. The action layer
# maps such a turn to PROVENANCE_BACKGROUND (mirrors skill_manage).
_NON_USER_AUTHORS = frozenset({PROVENANCE_BACKGROUND})

# The literal on-disk directory name under a data-home.
_PROFILES_SUBDIR = "profiles"
_PENDING_DIR = ".pending"
_ARCHIVED_DIR = ".archived"

# A path-safe worker id (reject-never-rewrite; blocks traversal). Same shape as a
# skill id: lowercase alnum + dash, must start with a letter.
_PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

# The default/builtin profile id — resolved by the legacy registry, never a
# tenant worker; kept dispatchable with the store OFF or empty.
DEFAULT_PROFILE_ID = "executor"


def workers_enabled() -> bool:
    """``WORKERS_ENABLED`` (default OFF). The single gate for the worker store.

    OFF ⇒ the delegation path never consults this store ⇒ ``delegate_task(profile=…)``
    is byte-identical to the pre-043 behavior.
    """
    from core.config_policy import _bool_env

    return _bool_env("WORKERS_ENABLED", False)


def _data_home() -> Path:
    """``<data_home>`` with the local-vs-server split applied — the ONE rule
    skill storage and the update snapshot/rollback paths also resolve through."""
    from core.runtime_paths import effective_data_home
    return effective_data_home()


def profiles_data_home() -> Path:
    """``<data_home>/profiles`` — the writable root for tenant worker profiles
    (db_manifest-style path SSOT: the ONE place the on-disk root is named)."""
    return _data_home() / _PROFILES_SUBDIR


def is_valid_profile_id(profile_id: str) -> bool:
    """True if ``profile_id`` is a path-safe worker id."""
    return bool(profile_id) and bool(_PROFILE_ID_RE.fullmatch(str(profile_id)))


class ProfileWriteResult:
    """Outcome of a worker write (mirrors SkillWriteResult's shape)."""

    def __init__(self, profile_id: str, ok: bool, *, errors=None, warnings=None,
                 pending: bool = False, path: Optional[str] = None,
                 quarantine_reason: Optional[str] = None):
        self.profile_id = profile_id
        self.ok = ok
        self.errors = errors or []
        self.warnings = warnings or []
        self.pending = pending
        self.path = path
        # Why the write was quarantined ("scan" | "forged" | "requested"), or None.
        self.quarantine_reason = quarantine_reason

    def __repr__(self) -> str:
        state = "pending" if self.pending else ("ok" if self.ok else "rejected")
        return f"<ProfileWriteResult {self.profile_id} {state} errors={self.errors}>"


class ProfileStore:
    """Create / read / list / remove tenant worker profiles.

    ``home_dir`` is the DATA-HOME root (worker files land under
    ``home_dir/profiles/user_{uid}/``). Pass a tmp dir in tests; ``None`` (the
    default) resolves the real data home via :func:`profiles_data_home`, so a
    unit test MUST inject ``home_dir`` to avoid touching the developer's data.
    """

    def __init__(self, home_dir: Union[Path, str, None] = None):
        self.home_dir = Path(home_dir) if home_dir is not None else None

    # --- paths ---------------------------------------------------------------

    def _profiles_base(self) -> Path:
        if self.home_dir is not None:
            return self.home_dir / _PROFILES_SUBDIR
        return profiles_data_home()

    def _root(self, uid: str) -> Path:
        return self._profiles_base() / f"user_{uid}"

    def _active_file(self, uid: str, profile_id: str) -> Path:
        return self._root(uid) / f"{profile_id}.json"

    def _pending_file(self, uid: str, profile_id: str) -> Path:
        return self._root(uid) / _PENDING_DIR / f"{profile_id}.json"

    def _archived_dir(self, uid: str) -> Path:
        return self._root(uid) / _ARCHIVED_DIR

    @staticmethod
    def _require_user(user_id: Optional[str]) -> Optional[str]:
        """Return a clean, path-safe user_id or None (anon-block; never write
        under a shared bucket, never let a traversal-shaped id build a path)."""
        from core.instance import is_safe_tenant_id

        if user_id is None:
            return None
        uid = str(user_id).strip()
        if not uid or not is_safe_tenant_id(uid):
            return None
        return uid

    # --- scan ----------------------------------------------------------------

    @staticmethod
    def _scan_forces_quarantine(body: str, description: str) -> bool:
        """Threat-scan the BODY and the DESCRIPTION separately, fail-CLOSED.

        Returns True (⇒ quarantine, not publish) when the scanner flags EITHER,
        when the scan RAISES, or when the scanner is unavailable — an unscanned
        worker must never auto-activate. Returns False only when both are proven
        clean.
        """
        try:
            from modules.memory.task.threat_scan import (
                is_skill_content_suspicious as is_suspicious,
            )
        except Exception:
            # Scanner absent — fail CLOSED (quarantine rather than publish
            # unscanned content). Matches WK1's "on a scan error → quarantine".
            logger.warning("worker write: threat scanner unavailable — quarantining (fail-closed)")
            return True
        for label, text in (("body", body), ("description", description)):
            if not text:
                continue
            try:
                if is_suspicious(text):
                    logger.warning("worker write: %s failed injection threat-scan — quarantining", label)
                    return True
            except Exception as e:
                logger.warning("worker write: %s scan raised — quarantining (fail-closed): %s", label, e)
                return True
        return False

    def _resolve_quarantine(self, created_by: str, pending: Optional[bool],
                            scan_forces: bool) -> tuple[bool, Optional[str]]:
        """Decide whether the write is quarantined and why.

        A non-user (forged/delegated/leaf) author OR a scan concern ALWAYS
        quarantines — regardless of the ``pending`` request. Otherwise an
        explicit ``pending`` request wins; a normal user/agent turn defaults to
        active.
        """
        if created_by in _NON_USER_AUTHORS:
            return True, "forged"
        if scan_forces:
            return True, "scan"
        if pending:
            return True, "requested"
        return False, None

    # --- write ---------------------------------------------------------------

    @staticmethod
    def _serialize_body(model: AgentProfileModel) -> str:
        """The scannable BODY = every field EXCEPT ``description`` (which is
        scanned separately). Deterministic so a test can reason about it."""
        data = model.model_dump()
        data.pop("description", None)
        return json.dumps(data, sort_keys=True, default=str)

    def _atomic_write(self, dest: Path, text: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _archive(self, uid: str, profile_id: str, src: Path) -> None:
        """Move a prior body to ``.archived/{id}.{n}.json`` (never destroy)."""
        adir = self._archived_dir(uid)
        adir.mkdir(parents=True, exist_ok=True)
        n = 1
        while (adir / f"{profile_id}.{n}.json").exists():
            n += 1
        os.replace(src, adir / f"{profile_id}.{n}.json")

    def save_profile(self, profile: Union[AgentProfileModel, Dict],
                     *, user_id: str, created_by: str = PROVENANCE_AGENT,
                     pending: Optional[bool] = None) -> ProfileWriteResult:
        """Author a tenant worker (validated, scanned, atomically written).

        ``profile`` is an :class:`AgentProfileModel` or a dict for one. ``created_by``
        carries the turn's provenance (the caller runs the forged/leaf detection);
        a ``PROVENANCE_BACKGROUND`` author is always quarantined and undispatchable.
        """
        uid = self._require_user(user_id)
        if uid is None:
            return ProfileWriteResult("", False,
                                      errors=["empty/unsafe user_id refused (tenant scope)"])

        try:
            model = (profile if isinstance(profile, AgentProfileModel)
                     else AgentProfileModel(**dict(profile)))
        except Exception as e:
            return ProfileWriteResult(str((profile or {}).get("id", "") if isinstance(profile, dict) else getattr(profile, "id", "")),
                                      False, errors=[f"invalid profile: {e}"])

        pid = model.id
        if not is_valid_profile_id(pid):
            return ProfileWriteResult(pid, False,
                                      errors=["invalid profile id (want ^[a-z][a-z0-9-]{0,63}$)"])

        body = self._serialize_body(model)
        description = model.description or ""
        scan_forces = self._scan_forces_quarantine(body, description)
        quarantine, reason = self._resolve_quarantine(created_by, pending, scan_forces)

        payload = json.dumps(model.model_dump(), indent=2, sort_keys=True, default=str)
        dest = self._pending_file(uid, pid) if quarantine else self._active_file(uid, pid)

        try:
            # Archive-never-delete: back up a prior body at the SAME destination
            # lane before overwriting it.
            if dest.is_file():
                self._archive(uid, pid, dest)
            # A promote-to-active also supersedes any pending draft — archive it.
            if not quarantine:
                pend = self._pending_file(uid, pid)
                if pend.is_file():
                    self._archive(uid, pid, pend)
            self._atomic_write(dest, payload)
            logger.info("authored worker %s/%s (%s%s)", uid, pid,
                        "pending" if quarantine else "active",
                        f": {reason}" if reason else "")
            return ProfileWriteResult(pid, True, pending=quarantine, path=str(dest),
                                      quarantine_reason=reason)
        except Exception as e:
            logger.error("worker write failed for %s/%s: %s", uid, pid, e, exc_info=True)
            return ProfileWriteResult(pid, False, errors=[f"write failed: {e}"])

    def remove_profile(self, profile_id: str, *, user_id: str) -> bool:
        """Archive-never-delete a worker (active + any pending draft)."""
        uid = self._require_user(user_id)
        if uid is None or not is_valid_profile_id(profile_id):
            return False
        moved = False
        for src in (self._active_file(uid, profile_id), self._pending_file(uid, profile_id)):
            if src.is_file():
                try:
                    self._archive(uid, profile_id, src)
                    moved = True
                except Exception as e:
                    logger.error("worker remove failed for %s/%s: %s", uid, profile_id, e)
        return moved

    # --- read ----------------------------------------------------------------

    def get_approved(self, profile_id: str, *, user_id: str) -> Optional[AgentProfileModel]:
        """Return the APPROVED worker, or None. A pending worker is NEVER
        returned (undispatchable) — pending drafts live under ``.pending/`` and
        this reads only the active lane."""
        uid = self._require_user(user_id)
        if uid is None or not is_valid_profile_id(profile_id):
            return None
        f = self._active_file(uid, profile_id)
        if not f.is_file():
            return None
        try:
            return AgentProfileModel(**json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning("approved worker %s/%s unreadable: %s", uid, profile_id, e)
            return None

    def list_approved(self, user_id: str) -> List[AgentProfileModel]:
        """Every APPROVED worker for a tenant (043 Capabilities›Helpers reads
        this). Excludes ``.pending/`` and ``.archived/`` (both are subdirs, so a
        top-level ``*.json`` glob skips them). Never touches another tenant."""
        uid = self._require_user(user_id)
        if uid is None:
            return []
        root = self._root(uid)
        if not root.is_dir():
            return []
        out: List[AgentProfileModel] = []
        for f in sorted(root.glob("*.json")):
            if not f.is_file() or f.name.startswith("."):
                continue
            try:
                out.append(AgentProfileModel(**json.loads(f.read_text(encoding="utf-8"))))
            except Exception as e:
                logger.warning("approved worker file %s unreadable: %s", f, e)
        return out

    def list_pending(self, user_id: str) -> List[str]:
        """Pending (quarantined, undispatchable) worker ids awaiting review."""
        uid = self._require_user(user_id)
        if uid is None:
            return []
        pdir = self._root(uid) / _PENDING_DIR
        if not pdir.is_dir():
            return []
        return sorted(f.stem for f in pdir.glob("*.json")
                      if f.is_file() and not f.name.startswith("."))


def worker_dispatch_refusal(profile_id: Optional[str],
                            user_id: Optional[str]) -> Optional[str]:
    """The ONE gated store lookup for the delegation path (043 §6).

    Returns a refusal string when ``WORKERS_ENABLED`` is ON and ``profile_id``
    names a non-default worker that is NOT an APPROVED tenant profile — i.e. it is
    unknown, or it is quarantined in ``.pending/`` (a forged/leaf-authored or
    suspicious worker is undispatchable). Returns ``None`` (ALLOW) when:

    - the store is OFF (default) — so ``delegate_task(profile=…)`` stays
      byte-identical to the pre-043 path;
    - the profile is the builtin default (``executor``);
    - the worker is approved for this tenant.

    Fail-OPEN: any error here allows the delegation (a store hiccup must never
    break a legitimate delegate).
    """
    try:
        if not profile_id or profile_id == DEFAULT_PROFILE_ID:
            return None
        if not workers_enabled():
            return None
        if get_store().get_approved(profile_id, user_id=user_id) is not None:
            return None
        return (f"worker profile '{profile_id}' is not an approved worker "
                f"(unknown, or pending review). Delegate with an approved worker "
                f"id or profile='executor'.")
    except Exception:
        logger.debug("worker_dispatch_refusal check failed (fail-open)", exc_info=True)
        return None


_default_store: Optional[ProfileStore] = None


def get_store() -> ProfileStore:
    """The process-wide store bound to the real data home (lazy singleton).

    NEVER bind at import time — the data home is resolved per call so a profile
    selection (`-P`) or `POLYROB_DATA_DIR` set after import is honored."""
    global _default_store
    if _default_store is None:
        _default_store = ProfileStore(home_dir=None)
    return _default_store
