"""Seed the bundled skill library into the data home (081 WS-5.3, owner Q3).

The package ships ``data/prompts/skills/`` (:func:`core.skills.scopes.package_skills_root`,
synced by the deploy with ``--delete``). Each process start copies it to
``<data_home>/skills/bundled/`` (:func:`core.skills.scopes.bundled_seed_root`) and
records ``id:sha256`` of every ``SKILL.md`` it copied in ``.bundled_manifest``.
Once the manifest exists the builtin scope serves the seeded copy, so a fix to a
bundled skill needs no deploy: the agent proposes it (a ``.pending/`` draft in its
tenant) and the owner accepts it (``promote``), which writes the seeded copy.

Each later start (and so each update) applies these rules, per skill:

- new upstream id: copied;
- unmodified copy (its sha is the manifest's): replaced when upstream changed;
- copy the owner changed (its sha is the last accepted ``promote`` in the ledger):
  KEPT, named in the report (``kept``); its resource files still follow upstream;
- copy changed outside an accept: archived and restored (the accepted revision
  when the archive holds it, else the package copy), named (``restored``);
- id that left the package: removed (a changed copy is archived first), named —
  the seeded root holds exactly the package's ids, as the ``--delete`` sync does;
- a tenant skill under a bundled id: untouched (the bundled skill serves the id,
  as before seeding), named (``collisions``) so the owner can rename it.

The report goes to the log, to ``.seed_report.json`` (read by the update's
"back online" message) and to ``polyrob update``'s own output.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from core.skills import scopes

logger = logging.getLogger(__name__)

SEED_REPORT = ".seed_report.json"
_LOCK = ".seed.lock"
_ACTOR = "seed"
_SKIP_NAMES = ("__pycache__",)


@dataclass
class SeedReport:
    seeded: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)
    restored: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    collisions: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)  # another process seeds (not an error)

    def owner_lines(self) -> List[str]:
        """What an owner must know after an update (empty when nothing needs it)."""
        out = []
        if self.kept:
            out.append("Bundled skills you changed were kept (the update did not replace "
                       f"them): {', '.join(self.kept)}.")
        if self.restored:
            out.append("Bundled skills changed outside an accepted fix were restored: "
                       f"{', '.join(self.restored)}.")
        if self.collisions:
            out.append("Your skills that share an id with a bundled skill (the bundled one "
                       f"is used; rename yours): {', '.join(self.collisions)}.")
        if self.errors:
            out.append(f"Skill seeding problems: {'; '.join(self.errors)}.")
        return out

    def summary(self) -> str:
        parts = [f"{k} {len(v)}" for k, v in asdict(self).items() if v]
        return ", ".join(parts) or "no change"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(root: Path) -> str:
    """sha256 over every file's relative path and bytes (no caches, no dot files)."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part.startswith(".") or part in _SKIP_NAMES for part in rel.parts):
            continue
        if p.is_file() and not p.is_symlink():
            h.update(str(rel).encode() + b"\0" + _sha(p).encode() + b"\n")
    return h.hexdigest()


@contextmanager
def seed_lock(root: Path):
    """Exclusive lock over the seeded root (seed runs and bundled promotes)."""
    import fcntl
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(root / _LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _replace_dir(src: Path, dest: Path, *, keep_skill_md: Optional[str] = None) -> None:
    """Copy ``src`` over ``dest`` (whole dir, atomic swap). ``keep_skill_md`` keeps
    that SKILL.md text in the new copy (a kept owner fix; resources follow upstream)."""
    tmp = dest.parent / f".tmp-{dest.name}-{os.getpid()}"
    old = dest.parent / f".old-{dest.name}-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".*"))
    if keep_skill_md is not None:
        (tmp / "SKILL.md").write_text(keep_skill_md, encoding="utf-8")
    if dest.exists():
        os.replace(dest, old)
    os.replace(tmp, dest)
    shutil.rmtree(old, ignore_errors=True)


def archive_body(root: Path, skill_id: str, text: str) -> str:
    """Keep ``text`` as ``<root>/.archived/<id>/<sha256>-SKILL.md``; the path."""
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    dest = root / ".archived" / skill_id / f"{sha}-SKILL.md"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)
    return str(dest)


def archived_body(root: Path, skill_id: str, sha: str) -> Optional[str]:
    path = root / ".archived" / skill_id / f"{sha}-SKILL.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def accepted_sha(skill_id: str) -> Optional[str]:
    """The sha of the owner's accepted revision of a bundled skill that still
    stands, else None: the newest ``promote``, unless a later seed row replaced the
    copy with upstream (``seed``/``remove``). Raises when the ledger cannot be read."""
    from modules.skills.skill_usage import get_skill_usage_store
    for row in get_skill_usage_store().list_changes(skill_id, scope="bundled", limit=50):
        if row.get("action") == "promote" and row.get("after_sha"):
            return row["after_sha"]
        if row.get("actor") == _ACTOR and row.get("action") in ("seed", "remove"):
            return None
    return None


def _ledger(skill_id: str, action: str, **row) -> None:
    try:
        from modules.skills.skill_usage import get_skill_usage_store
        get_skill_usage_store().record_change(skill_id, "", action=action, actor=_ACTOR,
                                              scope="bundled", **row)
    except Exception:
        logger.debug("seed ledger row skipped for %s", skill_id, exc_info=True)


def write_manifest(root: Path, manifest: Dict[str, str]) -> None:
    tmp = root / f"{scopes.SEED_MANIFEST}.tmp"
    tmp.write_text("".join(f"{k}:{v}\n" for k, v in sorted(manifest.items())), encoding="utf-8")
    os.replace(tmp, root / scopes.SEED_MANIFEST)


def _collisions(users_root: Path, ids: List[str]) -> List[str]:
    out = []
    try:
        tenants = sorted(d for d in users_root.iterdir()
                         if d.is_dir() and d.name.startswith("user_"))
    except OSError:
        return out
    for tenant in tenants:
        out.extend(f"{tenant.name}/{sid}" for sid in ids if (tenant / sid / "SKILL.md").is_file())
    return out


def _seed_one(sid: str, src: Path, root: Path, manifest: Dict[str, str], rep: SeedReport) -> None:
    dest = root / sid
    src_sha = _sha(src / "SKILL.md")
    origin = manifest.get(sid)
    cur_file = dest / "SKILL.md"
    if origin is None or not cur_file.is_file():
        if dest.exists():  # a stray copy the manifest never recorded
            archive_body(root, sid, cur_file.read_text(encoding="utf-8")) if cur_file.is_file() else None
        _replace_dir(src, dest)
        manifest[sid] = src_sha
        (rep.seeded if origin is None else rep.restored).append(sid)
        _ledger(sid, "seed" if origin is None else "restore", after_sha=src_sha)
        return
    cur_text = cur_file.read_text(encoding="utf-8")
    cur_sha = hashlib.sha256(cur_text.encode("utf-8")).hexdigest()
    if cur_sha == origin:  # unmodified: follow upstream
        if _tree_digest(src) != _tree_digest(dest):
            archive_body(root, sid, cur_text)  # the `update` row's before_sha: a rollback source
            _replace_dir(src, dest)
            manifest[sid] = src_sha
            rep.updated.append(sid)
            _ledger(sid, "update", before_sha=cur_sha, after_sha=src_sha)
        return
    try:
        accepted = accepted_sha(sid)
    except Exception as exc:  # cannot tell an accepted fix from a stray edit: leave it
        rep.kept.append(sid)
        rep.errors.append(f"{sid}: ledger unreadable ({type(exc).__name__}), copy left as is")
        return
    if accepted == cur_sha:  # the owner's fix: keep it; resources follow upstream
        rep.kept.append(sid)
        _replace_dir(src, dest, keep_skill_md=cur_text)
        return
    path = archive_body(root, sid, cur_text)
    body = archived_body(root, sid, accepted) if accepted else None
    if body is not None:
        _replace_dir(src, dest, keep_skill_md=body)
        after = accepted
    else:
        _replace_dir(src, dest)
        manifest[sid] = src_sha
        after = src_sha
    rep.restored.append(sid)
    _ledger(sid, "restore", before_sha=cur_sha, after_sha=after, archive_path=path)


def _not_this_process(path: Path) -> str:
    """Why this process must not seed under ``path`` ("" when it may).

    Root in a shell on a server whose data home belongs to the service group
    (prod: ``root:polyrob-data 2775``) must leave the seed to the service: files
    it copies keep the package modes and the service could not replace them.
    The service itself writes through that group, so ownership of ``path`` is
    not the test — prod 2026-10-15: an owner-uid check refused the service."""
    try:
        st = path.stat()
    except OSError:
        return ""
    if os.geteuid() == 0 and st.st_gid != 0:
        return f"root shell; {path} belongs to group {st.st_gid}, the service seeds it"
    if not os.access(path, os.W_OK | os.X_OK):
        return f"{path} is not writable by this process"
    return ""


def seed_bundled_skills(*, source: Optional[Path] = None, root: Optional[Path] = None,
                        users_root: Optional[Path] = None) -> SeedReport:
    """Seed or update the bundled copy. Never raises; problems are in the report."""
    rep = SeedReport()
    try:
        source = Path(source or scopes.package_skills_root())
        root = Path(root or scopes.bundled_seed_root())
        users_root = Path(users_root or root.parent)
        why = _not_this_process(root.parent)
        if why:
            rep.skipped.append(why)
            return rep
        ids = scopes.builtin_skill_ids(scopes.SkillScope(
            name="package", root=source, writable=False, trusted=True))
        if not ids:
            rep.errors.append(f"no bundled skills under {source}")
            return rep
        with seed_lock(root):
            first_seed = not (root / scopes.SEED_MANIFEST).is_file()
            manifest = scopes.read_seed_manifest(root)
            failed = []
            for sid in ids:
                try:
                    _seed_one(sid, source / sid, root, manifest, rep)
                except Exception as exc:
                    failed.append(sid)
                    rep.errors.append(f"{sid}: {type(exc).__name__}: {exc}")
            gone = set(manifest) | {d.name for d in root.iterdir()
                                    if d.is_dir() and not d.name.startswith((".", "user_"))}
            for sid in sorted(gone - set(ids)):
                cur = root / sid / "SKILL.md"
                try:
                    if cur.is_file():
                        text = cur.read_text(encoding="utf-8")
                        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                        path = archive_body(root, sid, text) if sha != manifest.get(sid) else None
                        _ledger(sid, "remove", before_sha=sha, archive_path=path)
                    shutil.rmtree(root / sid, ignore_errors=True)
                    manifest.pop(sid, None)
                    rep.removed.append(sid)
                except Exception as exc:
                    rep.errors.append(f"{sid}: {type(exc).__name__}: {exc}")
            if first_seed and failed:
                # The manifest switches serving to the copy; a skill that failed
                # to copy would be missing until the next seed. Keep serving the
                # package and retry the whole first seed next start.
                rep.errors.append(f"first seed incomplete ({', '.join(failed)}); "
                                  "the package keeps serving until the next seed")
            else:
                write_manifest(root, manifest)  # last: its presence switches serving
            rep.collisions = _collisions(users_root, ids)
            try:
                (root / SEED_REPORT).write_text(json.dumps(asdict(rep), indent=1), encoding="utf-8")
            except OSError:
                pass
    except Exception as exc:
        logger.warning("bundled skill seeding failed: %s", exc, exc_info=True)
        rep.errors.append(f"{type(exc).__name__}: {exc}")
    return rep


def last_report(root: Optional[Path] = None) -> Optional[SeedReport]:
    """The report the last seed wrote, or None."""
    try:
        data = json.loads((Path(root or scopes.bundled_seed_root()) / SEED_REPORT).read_text())
        return SeedReport(**{k: list(v) for k, v in data.items() if k in SeedReport.__annotations__})
    except Exception:
        return None


def main() -> int:
    """``python -m modules.skills.seed``: seed now and print the owner lines."""
    rep = seed_bundled_skills()
    print(f"bundled skills: {rep.summary()}")
    for line in rep.owner_lines():
        print(line)
    return 0 if not rep.errors else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
