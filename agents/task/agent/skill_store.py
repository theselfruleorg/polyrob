"""Skill storage scopes (Task 8 — data-safety fix).

Splits "where do skills live" into named, precedence-ordered SCOPES so writable
per-tenant skills can move OUT of the installed package/code tree and into
data-home, where they survive a ``polyrob update`` code-swap.

Before this module existed, ``SkillManager``/``SkillWriterMixin`` wrote
``user_<uid>/`` skills directly under the same directory that ships the
curated SYSTEM skills (``data/prompts/skills/``) — a directory that lives
INSIDE the installed code/package tree. ``polyrob update``'s code-swap
replaces that whole tree, so every self-authored skill a user/agent had built
up was silently destroyed on update.

Three named scopes, precedence **project > user > builtin**:

  - **project** — a per-repo ``.agents/skills`` / ``.claude/skills`` dir.
    STUB: Task 15 owns real discovery/loading; this module only reserves the
    scope + precedence slot so that later work doesn't have to re-litigate
    ordering. Not read from or written to yet.
  - **user**    — ``<data_home>/skills/user_<uid>/``. Writable; survives a
    code-tree swap because data-home is never touched by ``polyrob update``.
  - **builtin** — the shipped package ``data/prompts/skills/`` (the curated
    system-skill library). Read-only, pre-vetted/trusted.

``<data_home>`` is resolved via the EXACT same accessor
``cli/update/context.py`` uses for its own data-home
(``core/runtime_paths.py::resolve_runtime_paths``), so ``POLYROB_DATA_DIR`` is
honored identically across the update and skill-storage code paths.

NOTE on migration: this module only changes WHERE NEW reads/writes land. Any
``user_<uid>/`` directories that predate this change and still live under the
package tree are NOT moved by this module — that one-time migration is a
separate, deliberately-scoped follow-up (Task 9) so a data-moving operation
gets its own dedicated review.
"""

import errno
import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillScope:
    """One named root that skills can be read from / written to.

    ``trusted`` describes the LOCATION's provenance (pre-vetted shipped
    content vs. arbitrary/unreviewed content) — it is NOT a blanket "skip
    security scanning" flag. See :func:`scan_exempt`, which additionally
    requires ``not writable`` so a writable scope (today: user) can never
    have its write-time content scan suppressed just because the scope is
    marked trusted.
    """

    name: str
    root: Path
    writable: bool
    trusted: bool


# The literal on-disk directory name under a scope's data-home root.
_SKILLS_SUBDIR = "skills"


def _builtin_root() -> Path:
    """The installed package's shipped (builtin) skills directory.

    Resolved from THIS file's own location rather than importing
    ``skill_manager`` — the brief's suggested
    ``Path(skill_manager.__file__).resolve().parents[3]`` would create a
    ``skill_store`` <-> ``skill_manager`` import cycle, since ``skill_manager``
    needs ``skill_store`` for the user/data-home scope. ``skill_store.py``
    lives in the SAME directory as ``skill_manager.py``
    (``agents/task/agent/``), so the identical ``parents[3]`` hop from this
    file resolves to the same repo/package root:
    ``agents/task/agent/<this file>`` -> parents[0]=agent, [1]=task,
    [2]=agents, [3]=repo root. Verified empirically: this path exists and
    contains ``rules.json`` (see ``test_skill_store_scopes.py``).
    """
    return Path(__file__).resolve().parents[3] / "data" / "prompts" / "skills"


def _data_home() -> Path:
    """Resolve ``<data_home>`` the SAME way ``cli/update/context.py`` does,
    so ``POLYROB_DATA_DIR`` (and the local-vs-server default split) is
    honored identically for skill storage and for update snapshot/rollback.
    """
    from core.runtime_paths import resolve_runtime_paths

    try:
        from agents.task.constants import local_mode_enabled

        local = local_mode_enabled()
    except Exception:
        local = False
    rp = resolve_runtime_paths(local=local)
    return Path(rp.data_home)


def skills_data_home() -> Path:
    """``<data_home>/skills`` — the writable root for user-authored skills."""
    return _data_home() / _SKILLS_SUBDIR


def builtin_scope() -> SkillScope:
    """The shipped, read-only, pre-vetted system-skill library.

    NOTE: this scope's root MAY also physically hold legacy ``user_<uid>/``
    dirs at runtime until Task 9's migration moves them into ``user_scope()``.
    Callers enumerating "the builtin skills" MUST exclude ``user_*``/dotted
    entries (see :func:`builtin_skill_ids`) rather than assuming every child
    of this root is a system skill.
    """
    return SkillScope(name="builtin", root=_builtin_root(), writable=False, trusted=True)


def user_scope() -> SkillScope:
    """The writable, tenant-scoped, data-home skill root (survives updates)."""
    return SkillScope(name="user", root=skills_data_home(), writable=True, trusted=True)


def _find_project_root_stub() -> Path:
    """STUB project-skills root (Task 15 owns real discovery/loading).

    Walks CWD up to the git root looking for a conventional per-repo skills
    dir (``.agents/skills`` then ``.claude/skills``); falls back to
    ``<cwd>/.agents/skills`` (need not exist) so ``project_scope()`` always
    has a concrete ``root``. Bounded walk — never infinite even with an
    unusual filesystem layout.
    """
    cur = Path.cwd().resolve()
    for _ in range(64):
        for rel in (".agents/skills", ".claude/skills"):
            cand = cur / rel
            if cand.is_dir():
                return cand
        if (cur / ".git").exists():
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return Path.cwd().resolve() / ".agents" / "skills"


def project_scope() -> SkillScope:
    """Per-repo skills scope. `trusted` reflects the runtime trust gate so a caller that
    inspects the scope sees the real policy; discovery still gates on
    skill_discovery.trust_project_skills_effective().
    """
    from . import skill_discovery
    trusted = False
    try:
        trusted = skill_discovery.trust_project_skills_effective()
    except Exception:
        trusted = False
    root = _find_project_root_stub()
    return SkillScope(name="project", root=root, writable=False, trusted=trusted)


def resolve_scopes() -> List[SkillScope]:
    """All skill scopes in precedence order: project > user > builtin."""
    return [project_scope(), user_scope(), builtin_scope()]


def scan_exempt(scope: SkillScope) -> bool:
    """Whether content already resident in ``scope`` can skip a write-time
    threat re-scan (future-proofs e.g. a reindex of the shipped library).

    Only ``True`` for a read-only + trusted scope (today: builtin). A
    writable scope is NEVER exempt regardless of its ``trusted`` flag —
    ``trusted`` describes a root's provenance, not a license to skip scanning
    NEW content written into it.

    ``SkillWriterMixin.create_skill`` only ever targets ``user_scope()``
    (``writable=True``), so this evaluates to ``False`` on every live write
    call today — a deliberate no-op that keeps user-write scanning
    unconditional. It exists so a future builtin-library reindex can reuse
    one policy function instead of re-deriving it.
    """
    return scope.trusted and not scope.writable


def builtin_skill_ids(scope: Optional[SkillScope] = None) -> List[str]:
    """Sorted ids of the shipped system skills under ``scope`` (default:
    :func:`builtin_scope`), EXCLUDING ``user_*`` and dotted directories.

    Mirrors ``SkillManager._iter_authored_skill_dirs``'s filter so "what
    counts as a builtin skill" can't drift between the two call sites.
    """
    scope = scope or builtin_scope()
    root = scope.root
    if not root.is_dir():
        return []
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith((".", "user_")) and (d / "SKILL.md").exists()
    )


# =============================================================================
# Task 9: one-time legacy code-tree -> data-home migration
#
# Before Task 8, per-tenant ``user_<uid>/`` skills were written directly under
# the SAME shipped package tree that holds the curated system-skill library
# (``builtin_scope().root`` — now this module's ``legacy_root`` parameter).
# Task 8 redirected ``SkillManager``'s user-scope reads/writes to data-home
# instead; that alone makes any PRE-EXISTING ``user_<uid>/`` content on that
# tree instantly invisible (no ``polyrob update`` needed - simply running the
# new code stops looking there), and it would additionally be destroyed
# outright by the next ``polyrob update`` code-swap. This section moves that
# legacy content into data-home, once, so a deployment upgrading past Task 8
# doesn't lose it.
# =============================================================================

_MIGRATION_MARKER_NAME = ".migrated_v1"
_MIGRATION_LOCK_NAME = ".migrate.lock"
_SOURCE_RETAINED_BREADCRUMB = ".migrated_source_retained"


def _is_safe_component(name: str) -> bool:
    """Whether a single path-segment name is safe to treat as a migration
    source/destination component: no traversal (``.``/``..``), no embedded
    separators, not empty.

    Mirrors the SPIRIT of ``SkillManager.validate_skill_id``'s traversal
    protection without importing ``skill_manager`` (would create an import
    cycle - see the ``_builtin_root`` docstring above). Deliberately looser
    than the full id-format regex (which requires lowercase-start/hyphens
    only) so a legitimately-named pre-Task-8 skill isn't silently abandoned
    by migration merely for predating stricter id validation; the traversal
    guard here is a SECURITY check, not a format check.
    """
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return True


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _migrate_one_skill(source_dir: Path, dest_dir: Path) -> bool:
    """Move one skill directory from the legacy code tree into data-home.

    Never destructive: copy to a temp dir alongside ``dest_dir`` -> sha256
    verify the copied ``SKILL.md`` against the source -> place at ``dest_dir``
    -> ONLY THEN attempt to remove the source. Placement is a fast
    ``os.replace`` when same-filesystem; a CROSS-DEVICE placement (``EXDEV``:
    package tree and data-home on different volumes, or a Windows cross-drive
    move) falls back to a plain ``copytree`` that RE-VERIFIES the placed
    destination and then deliberately LEAVES the source in place (copy+verify,
    NO cross-volume remove — the brief's rule, and matching the EACCES stance).
    On the same-filesystem path, the source removal is best-effort: a
    ``PermissionError``/``OSError`` (read-only / root-owned site-packages
    install) is tolerated, the source is left with a breadcrumb at the
    destination, never raised, never retried.

    Returns True iff a NEW, verified copy was placed at ``dest_dir``
    (regardless of whether the legacy source could/should also be cleaned up).
    """
    if dest_dir.exists():
        logger.debug("skill migration: destination already exists, skipping %s", dest_dir)
        return False

    source_skill_md = source_dir / "SKILL.md"
    if source_skill_md.is_symlink() or not source_skill_md.is_file():
        return False

    try:
        source_hash = _sha256_file(source_skill_md)
    except Exception:
        logger.warning("skill migration: could not hash %s - skipped", source_skill_md)
        return False

    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix=".migrate_tmp_", dir=str(dest_dir.parent)))
    cross_volume = False
    try:
        tmp_skill_dir = tmp_root / dest_dir.name
        shutil.copytree(source_dir, tmp_skill_dir)

        tmp_skill_md = tmp_skill_dir / "SKILL.md"
        if not tmp_skill_md.is_file() or _sha256_file(tmp_skill_md) != source_hash:
            logger.warning("skill migration: copy verification failed for %s - skipped", source_dir)
            return False

        if dest_dir.exists():
            # Lost a race to a concurrent writer (e.g. a fresh agent-authored
            # skill at the same id) between the earlier check and now - never
            # clobber whatever just landed there.
            return False

        try:
            os.replace(str(tmp_skill_dir), str(dest_dir))
        except OSError as e:
            if getattr(e, "errno", None) != errno.EXDEV:
                raise
            # Cross-device: fall back to a plain copy INTO the destination.
            if dest_dir.exists():
                return False
            shutil.copytree(tmp_skill_dir, dest_dir)
            cross_volume = True
            # Re-verify the PLACED destination (not the tmp): a cross-volume
            # copy is exactly the case where a partial/corrupt write is most
            # likely, and because we are about to DELIBERATELY leave the source
            # (no cross-volume remove), the destination must be provably
            # correct. On mismatch, roll back the bad destination and bail so
            # nothing is counted and the source stays untouched.
            dest_skill_md = dest_dir / "SKILL.md"
            try:
                dest_ok = dest_skill_md.is_file() and _sha256_file(dest_skill_md) == source_hash
            except Exception:
                dest_ok = False
            if not dest_ok:
                logger.warning(
                    "skill migration: cross-volume copy verification failed for %s "
                    "- rolling back destination, leaving source", source_dir,
                )
                shutil.rmtree(dest_dir, ignore_errors=True)
                return False
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    if cross_volume:
        # Brief rule: cross-volume placement is copy+verify with NO source
        # removal (a cross-device move isn't atomic, and the source tree may
        # be read-only anyway). Leave the legacy source in place, exactly like
        # the EACCES branch, and drop a breadcrumb at the (writable) dest.
        logger.info(
            "skill migration: cross-volume copy of %s to %s verified; leaving "
            "the legacy source in place (no cross-device remove)",
            source_dir, dest_dir,
        )
        try:
            (dest_dir / _SOURCE_RETAINED_BREADCRUMB).write_text(
                f"cross-volume copy; source not removed: {source_dir}\n", encoding="utf-8"
            )
        except Exception:
            pass  # breadcrumb is best-effort only - never let it fail the migration
        return True

    # Same-filesystem rename path - now best-effort remove the legacy source.
    # Never crash or loop on a permission failure here; the data-home copy is
    # what matters going forward regardless of whether cleanup succeeds.
    try:
        shutil.rmtree(source_dir)
    except (PermissionError, OSError) as e:
        logger.info(
            "skill migration: copied %s to %s but could not remove the legacy "
            "source (%s) - leaving it in place; the data-home copy is authoritative",
            source_dir, dest_dir, e,
        )
        try:
            (dest_dir / _SOURCE_RETAINED_BREADCRUMB).write_text(
                f"source not removed: {source_dir} ({e})\n", encoding="utf-8"
            )
        except Exception:
            pass  # breadcrumb is best-effort only - never let it fail the migration
    return True


def _merge_legacy_user_rules(legacy_user_dir: Path, dest_user_dir: Path) -> None:
    """Best-effort merge of a legacy per-user ``rules.json`` into the
    data-home copy, filling in only keys ABSENT at the destination (an
    existing destination entry always wins - never clobbered).

    Without this, a migrated skill directory has no matching rule and
    ``SkillManager.get_skills_for_session`` never surfaces it: the body moves
    but stays permanently inert, which would defeat the point of migrating it
    at all. The legacy file is left in place untouched; this only enriches
    the destination copy. Fail-open: any parse/write error is logged and
    swallowed, never propagated.
    """
    legacy_rules_file = legacy_user_dir / "rules.json"
    if legacy_rules_file.is_symlink() or not legacy_rules_file.is_file():
        return
    try:
        legacy_rules = json.loads(legacy_rules_file.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("skill migration: could not parse legacy rules.json at %s", legacy_rules_file)
        return
    if not isinstance(legacy_rules, dict) or not legacy_rules:
        return

    dest_rules_file = dest_user_dir / "rules.json"
    dest_rules: dict = {}
    if dest_rules_file.is_file():
        try:
            loaded = json.loads(dest_rules_file.read_text(encoding="utf-8"))
        except Exception:
            logger.debug(
                "skill migration: existing dest rules.json unparseable at %s - leaving untouched",
                dest_rules_file,
            )
            return  # don't risk clobbering something we can't parse
        if isinstance(loaded, dict):
            dest_rules = loaded

    changed = False
    for skill_id, rule in legacy_rules.items():
        if skill_id not in dest_rules:
            dest_rules[skill_id] = rule
            changed = True
    if not changed:
        return

    dest_user_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest_user_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dest_rules, f, indent=2)
        os.replace(tmp_name, str(dest_rules_file))
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


# Dotted sibling dirs under ``user_<uid>/`` that hold real skill CONTENT and
# must migrate too (Task 9 review fix #2) — as opposed to bookkeeping dotted
# entries (``.migrate.lock``/``.migrated_v1``/``rules.json``), which are NOT
# skill dirs and are never walked as such. ``.pending`` = quarantined drafts
# awaiting review; ``.archived`` = recoverable prior/deleted bodies. Leaving
# these on the code tree means ``polyrob update`` destroys a user's pending
# drafts and their entire undo history.
_QUARANTINE_SUBDIRS = (".pending", ".archived")


def _migrate_skill_dirs_in(src_container: Path, dest_container: Path,
                           *, label: str) -> int:
    """Migrate every direct child skill directory of ``src_container`` into
    ``dest_container`` using the shared per-skill pipeline + guards. Returns
    the count placed. Never raises — a listing/per-item failure is logged and
    skipped so one bad entry can't abort the container.

    ``src_container`` is either a ``user_<uid>/`` dir (top-level skills) or one
    of its ``.pending``/``.archived`` quarantine subdirs; the identical
    symlink / ``..`` / skip-existing / EACCES guards apply in every case.
    """
    moved = 0
    if src_container.is_symlink() or not src_container.is_dir():
        return 0
    try:
        skill_dir_names = sorted(
            d.name
            for d in src_container.iterdir()
            if d.is_dir()
            and not d.is_symlink()
            and not d.name.startswith(".")
            and _is_safe_component(d.name)
            and (d / "SKILL.md").is_file()
            and not (d / "SKILL.md").is_symlink()
        )
    except Exception:
        logger.debug("skill migration: failed to list %s", src_container, exc_info=True)
        return 0

    for skill_name in skill_dir_names:
        try:
            if _migrate_one_skill(src_container / skill_name, dest_container / skill_name):
                moved += 1
        except Exception:
            logger.warning(
                "skill migration: failed migrating %s/%s (skipped, no crash)",
                label, skill_name, exc_info=True,
            )
            continue
    return moved


def _migrate_pass(legacy_root: Path, home: Path) -> int:
    """Walk ``legacy_root`` for ``user_*`` dirs and migrate their skills into
    ``home`` (``skills_data_home()``). Returns the count of skill directories
    actually placed at a new destination. Never raises - any per-item failure
    is logged and skipped so one bad entry can't abort the whole pass.

    Per ``user_<uid>/`` this migrates the active (top-level) skills AND the
    ``.pending``/``.archived`` quarantine content (all user data), plus a
    best-effort ``rules.json`` merge.
    """
    moved = 0
    try:
        if legacy_root.is_symlink() or not legacy_root.is_dir():
            return 0
        user_dir_names = sorted(
            d.name
            for d in legacy_root.iterdir()
            if d.is_dir()
            and not d.is_symlink()
            and d.name.startswith("user_")
            and _is_safe_component(d.name)
        )
    except Exception:
        logger.debug("skill migration: failed to list legacy_root %s", legacy_root, exc_info=True)
        return 0

    for name in user_dir_names:
        user_dir = legacy_root / name
        dest_user_dir = home / name

        # Active (top-level) skills.
        moved += _migrate_skill_dirs_in(user_dir, dest_user_dir, label=name)

        # Quarantine content (.pending drafts + .archived history) — same
        # pipeline + guards, preserving the subdir layout in data-home.
        for sub in _QUARANTINE_SUBDIRS:
            moved += _migrate_skill_dirs_in(
                user_dir / sub, dest_user_dir / sub, label=f"{name}/{sub}"
            )

        try:
            _merge_legacy_user_rules(user_dir, dest_user_dir)
        except Exception:
            logger.debug("skill migration: rules.json merge skipped for %s", user_dir, exc_info=True)

    return moved


def migrate_legacy_user_skills(legacy_root: Path, *, lock_timeout: float = 5.0) -> int:
    """One-time migration of pre-Task-8 code-tree ``user_<uid>/`` skills into
    data-home (Task 9). Returns the count of skill directories moved.

    Safety properties (see module section header for the "why"):

      - **Idempotent**: a ``<data_home>/skills/.migrated_v1`` marker is
        written after a completed pass; any later call returns 0 immediately
        without re-scanning ``legacy_root``.
      - **Single-flight / concurrency-safe**: the whole pass runs under a
        :class:`~agents.task.utils.SafeFileLock` at
        ``<data_home>/skills/.migrate.lock`` so parallel workers/processes
        booting at once don't race each other. On contention/timeout this
        returns 0 rather than blocking indefinitely or racing.
      - **Resumable**: the marker is only written at the END of a pass, so an
        interrupted first attempt (process killed mid-migration) simply
        re-scans on the next call - already-placed destinations are detected
        via the per-skill "skip if dest exists" check and not re-copied.
      - **Traversal/symlink-safe**: never follows a symlinked ``user_*`` dir,
        skill dir, or ``SKILL.md``; rejects unsafe path components.
      - **Never clobbers**: a destination that already exists (e.g. a fresher
        data-home copy, or one created by a concurrent writer) is left alone
        and not counted as moved.
      - **EACCES-tolerant**: a read-only / root-owned legacy tree (typical
        site-packages install) means the source can't be removed after
        copying - that failure is swallowed, the source is left in place with
        a breadcrumb, and migration continues (never loops, never crashes).
      - **Fail-open**: any unexpected exception anywhere in this call is
        logged and swallowed; the count accumulated so far is returned. This
        function must NEVER raise - it's called from ``SkillManager.__init__``
        and must never block construction.
      - **Zero-touch when nothing to migrate**: if ``legacy_root`` (the
        package tree) has NO ``user_*`` dirs, returns 0 *before* resolving or
        creating data-home / the lock / the marker — so constructing a bare
        ``SkillManager()`` on a machine that never had legacy user skills
        never writes anything under the real ``~/.polyrob`` (or wherever
        data-home resolves). The pre-check is on the RAW ``legacy_root``, so
        it's environment-independent.
    """
    moved = 0
    try:
        # Cheap, env-independent pre-check on the package tree BEFORE touching
        # data-home (Task 9 review fix #3). No ``user_*`` dirs => nothing to
        # migrate => return immediately, creating no lock/marker/skills dir.
        # (Idempotency for the "had skills, already migrated" case is still the
        # marker's job below; this only short-circuits the empty case.)
        try:
            has_user_dirs = any(
                p.is_dir() and not p.is_symlink()
                for p in legacy_root.glob("user_*")
            )
        except OSError:
            has_user_dirs = False
        if not has_user_dirs:
            return 0

        home = skills_data_home()
        home.mkdir(parents=True, exist_ok=True)
        marker = home / _MIGRATION_MARKER_NAME
        if marker.exists():
            return 0

        from agents.task.utils import SafeFileLock

        lock_path = home / _MIGRATION_LOCK_NAME
        try:
            with SafeFileLock(str(lock_path), timeout=lock_timeout):
                if marker.exists():
                    # Another process finished while we waited for the lock.
                    return 0
                # A missing/symlinked legacy_root means we never actually got
                # to inspect anything - don't mark the migration "done" in
                # that case, so a legacy tree that appears later (unusual,
                # but cheap to stay open to) can still be picked up.
                root_was_inspectable = legacy_root.is_dir() and not legacy_root.is_symlink()
                moved = _migrate_pass(legacy_root, home)
                if not root_was_inspectable:
                    return moved
                try:
                    marker.write_text(f"migrated {moved} skill(s)\n", encoding="utf-8")
                except Exception:
                    logger.debug(
                        "skill migration: could not write completion marker "
                        "(will re-scan on next boot)", exc_info=True,
                    )
        except TimeoutError:
            logger.info("skill migration: lock at %s contended - skipping this attempt", lock_path)
            return 0
    except Exception:
        logger.debug("skill migration: unexpected error - skipping (fail-open)", exc_info=True)
    return moved
