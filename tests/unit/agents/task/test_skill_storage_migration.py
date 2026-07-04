"""Task 9 — one-time migration of legacy code-tree user_<uid>/ skills into data-home.

Task 8 moved SkillManager's per-tenant WRITE (and READ) target for user_<uid>/
skills to <data_home>/skills/ (skill_store.skills_data_home()), so a
`polyrob update` code-swap doesn't destroy them. That alone left a gap: any
user_<uid>/ directory that already existed under the shipped package tree
(data/prompts/skills/user_<uid>/, pre-Task-8's location) is now invisible AND
would be destroyed outright by the next update. These tests pin the one-time
catch-up migration (skill_store.migrate_legacy_user_skills) that closes it:
idempotent, single-flight-locked, resumable, traversal/symlink-safe, and
tolerant of a read-only/root-owned legacy tree (site-packages install).
"""
import json
from pathlib import Path

import pytest


# --- Step 1 (brief): the given failing test -----------------------------------

def test_migration_moves_user_skills_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    legacy = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_42" / "mine"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("---\nname: mine\ndescription: d\n---\n# b")

    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )
    dest = tmp_path / "home" / "skills" / "user_42" / "mine" / "SKILL.md"
    assert dest.exists() and moved == 1
    # Legacy source was removed (normal tmp_path permissions allow it).
    assert not legacy.exists()

    # Idempotent: a second call is a no-op (marker short-circuits).
    assert skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    ) == 0


def test_migration_content_is_byte_identical_after_move(tmp_path, monkeypatch):
    """The sha256-verify step must preserve content exactly (not just move the
    directory) - this is the entire point of verifying before removing."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    body = "---\nname: mine\ndescription: d\n---\n# heading\n\nSome body text.\n"
    legacy = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_42" / "mine"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(body)

    skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )
    dest = tmp_path / "home" / "skills" / "user_42" / "mine" / "SKILL.md"
    assert dest.read_text() == body


# --- Step 2 (job list item 2a): read-only source (EACCES on removal) ---------

def test_migration_tolerates_permission_error_removing_source(tmp_path, monkeypatch):
    """A read-only / root-owned legacy tree (site-packages install) must not
    block migration: the destination copy still lands, the source is left in
    place, and the function must not raise or loop."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    legacy = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_7" / "readonly-skill"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("---\nname: readonly-skill\n---\n# body\n")

    def _fake_rmtree(path, *args, **kwargs):
        if kwargs.get("ignore_errors"):
            return  # let temp-dir cleanup succeed silently (unaffected by the fault)
        raise PermissionError(f"[Errno 13] Permission denied: '{path}'")

    monkeypatch.setattr(skill_store.shutil, "rmtree", _fake_rmtree)

    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )

    dest = tmp_path / "home" / "skills" / "user_7" / "readonly-skill" / "SKILL.md"
    assert dest.exists(), "migrated copy must exist in data-home even if source removal fails"
    assert moved == 1
    # Source is left in place (not removed) - no crash, no data loss.
    assert legacy.exists(), "source must be left in place when removal fails"
    assert (legacy / "SKILL.md").exists()
    # A breadcrumb is left at the (writable) destination side, not the source.
    breadcrumb = tmp_path / "home" / "skills" / "user_7" / "readonly-skill" / ".migrated_source_retained"
    assert breadcrumb.exists()


# --- Step 2 (job list item 2b): already-exists-dest (skip, don't clobber) ----

def test_migration_skips_existing_destination_without_clobbering(tmp_path, monkeypatch):
    """A skill that already has a (possibly newer) copy at the data-home
    destination must be left alone - migration must never overwrite it."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    legacy = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_9" / "dup-skill"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# legacy body (should NOT win)\n")

    dest_dir = tmp_path / "home" / "skills" / "user_9" / "dup-skill"
    dest_dir.mkdir(parents=True)
    (dest_dir / "SKILL.md").write_text("# newer data-home body (must survive)\n")

    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )

    assert moved == 0, "an already-present destination must not be counted as moved"
    assert (dest_dir / "SKILL.md").read_text() == "# newer data-home body (must survive)\n"
    # Legacy source is untouched too (skip entirely, don't half-migrate).
    assert (legacy / "SKILL.md").read_text() == "# legacy body (should NOT win)\n"


# --- rules.json merge (value-add beyond the literal brief; see task report) --

def test_migration_merges_legacy_rules_without_clobbering_dest(tmp_path, monkeypatch):
    """A migrated skill needs its rules.json entry too, or it stays permanently
    invisible to SkillManager.get_skills_for_session (no rule => never
    trigger-matched, even though the SKILL.md body did move). The merge must
    fill in missing keys only - an existing dest entry always wins."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    user_dir = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_5"
    skill_dir = user_dir / "mine"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# mine\nbody\n")
    (user_dir / "rules.json").write_text(json.dumps({
        "mine": {"description": "legacy rule", "priority": 4, "auto_activate": True},
        "already-there": {"description": "should not overwrite dest"},
    }))

    dest_user_dir = tmp_path / "home" / "skills" / "user_5"
    dest_user_dir.mkdir(parents=True)
    (dest_user_dir / "rules.json").write_text(json.dumps({
        "already-there": {"description": "NEWER dest value must survive"},
    }))

    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )
    assert moved == 1

    merged = json.loads((dest_user_dir / "rules.json").read_text())
    assert merged["mine"]["description"] == "legacy rule"
    assert merged["already-there"]["description"] == "NEWER dest value must survive"

    # Legacy rules.json is left in place untouched (only enriches destination).
    assert json.loads((user_dir / "rules.json").read_text())["mine"]["description"] == "legacy rule"


def test_migration_rules_merge_is_skipped_when_no_legacy_rules_file(tmp_path, monkeypatch):
    """No legacy rules.json at all (Step 1's exact scenario) must not error -
    the merge step is a no-op, not a failure."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    legacy = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_1" / "mine"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# mine\nbody\n")

    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )
    assert moved == 1
    dest_rules = tmp_path / "home" / "skills" / "user_1" / "rules.json"
    assert not dest_rules.exists()


# --- Traversal / symlink refusal ----------------------------------------------

def test_migration_refuses_to_follow_symlinked_user_dir(tmp_path, monkeypatch):
    """A symlinked user_<uid> dir must never be followed - protects against a
    symlink planted to escape the legacy tree."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    outside = tmp_path / "outside_secret"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# should never be migrated\n")

    legacy_root = tmp_path / "pkg" / "data" / "prompts" / "skills"
    legacy_root.mkdir(parents=True)
    link = legacy_root / "user_evil"
    link.symlink_to(outside, target_is_directory=True)

    moved = skill_store.migrate_legacy_user_skills(legacy_root=legacy_root)
    assert moved == 0
    dest = tmp_path / "home" / "skills" / "user_evil"
    assert not dest.exists(), "a symlinked user dir must never be followed/migrated"


def test_migration_refuses_symlinked_skill_dir(tmp_path, monkeypatch):
    """A symlinked skill dir (within an otherwise-legit user dir) must never
    be followed either."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    outside = tmp_path / "outside_skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# should never be migrated\n")

    user_dir = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_3"
    user_dir.mkdir(parents=True)
    link = user_dir / "sneaky"
    link.symlink_to(outside, target_is_directory=True)

    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )
    assert moved == 0
    dest = tmp_path / "home" / "skills" / "user_3" / "sneaky"
    assert not dest.exists()


def test_migration_ignores_non_skill_and_unrelated_dotted_dirs(tmp_path, monkeypatch):
    """A bare dir with no SKILL.md, and an UNRELATED dotted dir (NOT
    .pending/.archived — e.g. a stray .weird, or the migration's own
    .migrate.lock/.migrated_v1 bookkeeping) must never be treated as skills to
    migrate. Only a direct child with a SKILL.md counts (mirroring
    SkillManager._iter_authored_skill_dirs); the .pending/.archived quarantine
    dirs ARE migrated, but by their own dedicated test below."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    user_dir = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_4"
    # Unrelated dotted dir that LOOKS like it holds a skill but must be ignored
    # (only .pending/.archived are recognised quarantine content).
    (user_dir / ".weird" / "not-a-real-skill").mkdir(parents=True)
    (user_dir / ".weird" / "not-a-real-skill" / "SKILL.md").write_text("# nope\n")
    # A non-skill dir (no SKILL.md).
    (user_dir / "no-skill-md").mkdir(parents=True)
    (user_dir / "no-skill-md" / "notes.txt").write_text("not a skill\n")
    # The one real, active skill.
    real = user_dir / "real-skill"
    real.mkdir()
    (real / "SKILL.md").write_text("# real\n")

    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )
    assert moved == 1
    home_user_dir = tmp_path / "home" / "skills" / "user_4"
    assert (home_user_dir / "real-skill" / "SKILL.md").exists()
    assert not (home_user_dir / ".weird").exists()
    assert not (home_user_dir / "no-skill-md").exists()


def test_migration_moves_pending_and_archived_skill_content(tmp_path, monkeypatch):
    """Task 9 review fix #2: .pending drafts and .archived history are user
    data too — they must migrate into data-home (preserving the subdir
    layout), not be left on the code tree for `polyrob update` to destroy.
    Uses the SAME copy→verify→replace→remove pipeline + guards as active
    skills."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    root = tmp_path / "pkg" / "data" / "prompts" / "skills"
    pending = root / "user_7" / ".pending" / "draft"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_text("# draft\nquarantined\n")
    archived = root / "user_7" / ".archived" / "old"
    archived.mkdir(parents=True)
    (archived / "SKILL.md").write_text("# old\narchived history\n")

    moved = skill_store.migrate_legacy_user_skills(legacy_root=root)
    assert moved == 2

    home_user = tmp_path / "home" / "skills" / "user_7"
    assert (home_user / ".pending" / "draft" / "SKILL.md").read_text() == "# draft\nquarantined\n"
    assert (home_user / ".archived" / "old" / "SKILL.md").read_text() == "# old\narchived history\n"
    # Sources removed on a normal (writable, same-fs) tmp tree.
    assert not pending.exists()
    assert not archived.exists()


def test_migration_pending_archived_refuse_symlinks(tmp_path, monkeypatch):
    """The .pending/.archived walk applies the SAME symlink guard — a symlinked
    entry inside a quarantine dir must never be followed."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    outside = tmp_path / "outside_q"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# should never be migrated\n")

    root = tmp_path / "pkg" / "data" / "prompts" / "skills"
    pending = root / "user_2" / ".pending"
    pending.mkdir(parents=True)
    (pending / "sneaky").symlink_to(outside, target_is_directory=True)

    moved = skill_store.migrate_legacy_user_skills(legacy_root=root)
    assert moved == 0
    assert not (tmp_path / "home" / "skills" / "user_2" / ".pending" / "sneaky").exists()


# --- Cross-volume (EXDEV) fallback: copy+verify, NEVER remove source ----------

def test_migration_cross_volume_fallback_keeps_source_and_verifies(tmp_path, monkeypatch):
    """Task 9 review fix #1: when os.replace raises EXDEV (package tree and
    data-home on different volumes / a Windows cross-drive move), migration
    must fall back to copy, RE-VERIFY the placed destination, and LEAVE the
    source in place (no cross-volume remove) — dest created + intact, source
    still present, no crash."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    import errno as _errno
    from agents.task.agent import skill_store

    legacy = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_8" / "xvol-skill"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# xvol\nbody\n")

    real_replace = skill_store.os.replace

    def _fake_replace(src, dst, *a, **k):
        # Force EXDEV only for the skill-dir placement (temp→dest), so any
        # other os.replace (e.g. an atomic rules.json write) is unaffected.
        if str(dst).endswith("xvol-skill"):
            raise OSError(_errno.EXDEV, "cross-device link")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(skill_store.os, "replace", _fake_replace)

    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )
    assert moved == 1
    dest = tmp_path / "home" / "skills" / "user_8" / "xvol-skill" / "SKILL.md"
    assert dest.exists()
    assert dest.read_text() == "# xvol\nbody\n"  # re-verified, intact
    # Source is STILL present — a cross-volume placement must not remove it.
    assert legacy.exists()
    assert (legacy / "SKILL.md").exists()
    # Breadcrumb dropped at the (writable) destination side.
    assert (dest.parent / ".migrated_source_retained").exists()


def test_migration_cross_volume_verification_failure_rolls_back(tmp_path, monkeypatch):
    """If the cross-volume fallback copy lands but does NOT verify, migration
    must roll back the bad destination and NOT count it — and never touch the
    source (it's left intact by definition on the EXDEV path)."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    import errno as _errno
    from agents.task.agent import skill_store

    legacy = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_8" / "corrupt-skill"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# real\nbody\n")

    real_replace = skill_store.os.replace
    real_copytree = skill_store.shutil.copytree

    def _fake_replace(src, dst, *a, **k):
        if str(dst).endswith("corrupt-skill"):
            raise OSError(_errno.EXDEV, "cross-device link")
        return real_replace(src, dst, *a, **k)

    def _corrupting_copytree(src, dst, *a, **k):
        # Simulate a partial/corrupt cross-volume write: the fallback copy
        # INTO the final destination lands with wrong content.
        result = real_copytree(src, dst, *a, **k)
        if str(dst).endswith("corrupt-skill"):
            (Path(dst) / "SKILL.md").write_text("# CORRUPTED\n")
        return result

    monkeypatch.setattr(skill_store.os, "replace", _fake_replace)
    monkeypatch.setattr(skill_store.shutil, "copytree", _corrupting_copytree)

    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )
    assert moved == 0, "a cross-volume copy that fails verification must not be counted"
    dest_dir = tmp_path / "home" / "skills" / "user_8" / "corrupt-skill"
    assert not dest_dir.exists(), "bad destination must be rolled back"
    # Source untouched.
    assert (legacy / "SKILL.md").read_text() == "# real\nbody\n"


# --- Zero-touch when there is nothing to migrate ------------------------------

def test_migration_no_user_dirs_does_not_touch_data_home(tmp_path, monkeypatch):
    """Task 9 review fix #3: with a legacy_root that has NO user_* dirs,
    migration returns 0 and creates NO data-home skills dir / marker / lock —
    so constructing a bare SkillManager() on a machine that never had legacy
    user skills never writes into the real ~/.polyrob."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    root = tmp_path / "pkg" / "data" / "prompts" / "skills"
    root.mkdir(parents=True)
    # System skills + rules.json present, but NO user_* dirs.
    (root / "some-system-skill").mkdir()
    (root / "some-system-skill" / "SKILL.md").write_text("# sys\n")
    (root / "rules.json").write_text("{}")

    moved = skill_store.migrate_legacy_user_skills(legacy_root=root)
    assert moved == 0
    skills_home = tmp_path / "home" / "skills"
    assert not skills_home.exists(), (
        "data-home skills dir (with its .migrate.lock/.migrated_v1) must not be "
        "created when there is nothing to migrate"
    )


# --- Lock: fail-open on contention --------------------------------------------

def test_migration_returns_zero_when_lock_acquisition_times_out(tmp_path, monkeypatch):
    """Single-flight: if the migration lock can't be acquired (another
    worker/process holds it), this call must give up quickly (fail-open)
    rather than block indefinitely, crash, or race the other holder."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    legacy = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_1" / "mine"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# mine\nbody\n")

    class _AlwaysTimesOut:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise TimeoutError("simulated contention")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("agents.task.utils.SafeFileLock", _AlwaysTimesOut)

    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )
    assert moved == 0
    # Nothing migrated while contended - legacy content is untouched, and no
    # marker was written (a future call may retry).
    assert (legacy / "SKILL.md").exists()
    assert not (tmp_path / "home" / "skills" / ".migrated_v1").exists()


# --- Misc fail-open edges -----------------------------------------------------

def test_migration_returns_zero_when_legacy_root_missing(tmp_path, monkeypatch):
    """legacy_root not existing at all (e.g. a fresh install with no prior
    user skills) must be a clean no-op, not an error - and must not mark the
    migration as permanently done (so a later-appearing tree can still be
    picked up)."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"  # never created
    )
    assert moved == 0
    assert not (tmp_path / "home" / "skills" / ".migrated_v1").exists()


def test_migration_never_raises_on_unexpected_error(tmp_path, monkeypatch):
    """Fail-open: an unexpected exception anywhere inside the pass must never
    propagate out of migrate_legacy_user_skills."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_store

    legacy = tmp_path / "pkg" / "data" / "prompts" / "skills" / "user_1" / "mine"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# mine\nbody\n")

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(skill_store, "_migrate_pass", _boom)

    # Must not raise.
    moved = skill_store.migrate_legacy_user_skills(
        legacy_root=tmp_path / "pkg" / "data" / "prompts" / "skills"
    )
    assert moved == 0


def test_migrate_legacy_user_skills_called_once_at_skill_manager_init(tmp_path, monkeypatch):
    """SkillManager.__init__ must invoke the migration exactly once, passing
    the REAL package builtin dir (never a test's skills_dir override) as
    legacy_root, and must never let a migration failure block construction."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task.agent import skill_manager as sm_mod

    calls = []
    monkeypatch.setattr(
        sm_mod.skill_store, "migrate_legacy_user_skills",
        lambda legacy_root, **k: calls.append(legacy_root) or 0,
    )

    sm = sm_mod.SkillManager(skills_dir=tmp_path / "override")
    assert len(calls) == 1
    assert calls[0] == sm_mod.skill_store.builtin_scope().root
    assert calls[0] != (tmp_path / "override")


def test_migrate_legacy_user_skills_failure_does_not_block_construction(tmp_path, monkeypatch):
    """A raising migration function must not prevent SkillManager() from
    being constructed."""
    from agents.task.agent import skill_manager as sm_mod

    def _boom(legacy_root, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(sm_mod.skill_store, "migrate_legacy_user_skills", _boom)

    sm = sm_mod.SkillManager(skills_dir=tmp_path)
    assert sm is not None
