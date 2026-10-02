"""polyrob skill hub — taps, search, install-by-name, the skill lock, the trust
matrix and update (067 P6).

This module adds DISTRIBUTION on top of the ONE install pipeline in
``cli/commands/skill_install.py``. It never fetches a skill, scans it, stages it
or promotes it itself: every install and every update ends in
``skill_install.dispatch_install`` (clone/fetch → audit → threat scan →
``.pending/`` quarantine → ``_approve``). What lives here:

* **Taps** — ``<skills-root>/.hub/taps.json`` (the skills root is the SAME
  data-home root the skill store uses: ``SkillManager._user_dirs_root()``).
  Two default taps are merged in at read time and never written to the file:
  ``theselfruleorg/polyrob-skills`` (tier ``official``) and ``anthropics/skills``
  (tier ``trusted``). A tap the operator adds is ``community``. There is no
  aggregator source (ClawHavoc).
* **Search** — a tap's ``skills/index.json`` (list of ``{name, description,
  path}``, ``path`` relative to the tap root), else ONE GitHub git-trees call
  that lists the ``SKILL.md`` directories. Fetched over the pipeline's own
  https-only opener (``skill_install._https_open``), with no credential header,
  a size cap and a timeout; cached under ``.hub/cache/`` for ``CACHE_TTL_S``.
  A tap that cannot be reached is a named line, never a crash.
* **Install by name** — ``<tap>/<skill>`` or a bare ``<skill>`` resolves to the
  git shorthand ``owner/repo/<path>`` and goes through ``dispatch_install`` with
  an ``InstallOrigin`` that carries the tap's tier.
* **Lock** — ``<skills-root>/.hub/lock.json``, keyed by tenant/name, written by ``_approve`` and
  cleared by ``remove``. Every entry is validated on write AND on read (name
  matches the skill-id rule; ``install_path`` resolves inside the user skill
  scope, as ``user_<uid>/<name>``). A bad entry is ignored with a logged reason
  and is never followed (a known rmtree-escape regression class).
* **Trust matrix** (proposal 067 §6 item 7) — ``trust_decision``.
* **Update** — re-stage from the recorded source at head, compare hashes, show
  a diff summary, then apply the trust matrix again.

Import cost: stdlib + click only at module import (the ``skill`` group is a
lazy subcommand of ``polyrob``).
"""
import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

logger = logging.getLogger(__name__)

HUB_DIRNAME = ".hub"
LOCK_VERSION = 2
TAPS_VERSION = 1
CACHE_TTL_S = 3600
_MAX_INDEX_BYTES = 1024 * 1024
_HTTP_TIMEOUT_S = 15

# Tier per default tap. Merged at read time, never written to taps.json.
DEFAULT_TAPS: Tuple[Tuple[str, str], ...] = (
    ("theselfruleorg/polyrob-skills", "official"),
    ("anthropics/skills", "trusted"),
)
AUTO_APPROVE_TIERS = frozenset({"official", "trusted"})

_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _si():
    """The install pipeline module (lazy: it imports this module back)."""
    from cli.commands import skill_install
    return skill_install


def _err(msg: str):
    return _si().InstallError(msg)


def skills_root() -> Path:
    """The root that holds ``user_<uid>/`` — the skill store's own resolution."""
    return _si()._skill_manager()._user_dirs_root()


def hub_dir() -> Path:
    return skills_root() / HUB_DIRNAME


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# --- Taps ------------------------------------------------------------------

@dataclass(frozen=True)
class Tap:
    id: str
    tier: str
    default: bool = False

    @property
    def owner(self) -> str:
        return self.id.split("/")[0]

    @property
    def repo(self) -> str:
        return self.id.split("/")[1]

    @property
    def subdir(self) -> str:
        return "/".join(self.id.split("/")[2:])


def normalize_tap(spec: str) -> str:
    """Validate ``owner/repo[/subdir]`` and return its canonical form.

    Every component must be a plain name (no ``.``/``..``, no URL, no ``@ref``):
    a tap is a GitHub location, and its id is later joined into URLs and a
    cache file name."""
    s = (spec or "").strip().strip("/")
    if s.endswith(".git"):
        s = s[:-4]
    parts = s.split("/")
    if len(parts) < 2 or any(
        not p or p in (".", "..") or not _COMPONENT_RE.match(p) for p in parts
    ):
        raise _err(f"invalid tap {spec!r}: expected owner/repo[/subdir]")
    return "/".join(parts)


def _taps_path() -> Path:
    return hub_dir() / "taps.json"


def _read_user_taps() -> List[str]:
    """The operator-added taps. A missing file is an empty list; an unreadable
    or malformed file is a NAMED error (an unreadable store is not an empty
    one), never silently treated as empty."""
    p = _taps_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        taps = data.get("taps") if isinstance(data, dict) else None
        if not isinstance(taps, list):
            raise ValueError("no 'taps' list")
    except Exception as e:
        raise _err(f"cannot read {p}: {e} — fix or delete the file")
    out: List[str] = []
    for t in taps:
        try:
            out.append(normalize_tap(str(t)))
        except Exception:
            logger.warning("skill hub: ignoring invalid tap entry %r in %s", t, p)
    return out


def list_taps() -> List[Tap]:
    defaults = {tid.lower() for tid, _ in DEFAULT_TAPS}
    taps = [Tap(tid, tier, default=True) for tid, tier in DEFAULT_TAPS]
    seen = set(defaults)
    for tid in _read_user_taps():
        if tid.lower() in seen:
            continue
        seen.add(tid.lower())
        taps.append(Tap(tid, "community"))
    return taps


def find_tap(tap_id: str) -> Optional[Tap]:
    want = tap_id.strip().strip("/").lower()
    for t in list_taps():
        if t.id.lower() == want:
            return t
    return None


def add_tap(spec: str) -> Tuple[Tap, bool]:
    """Add a community tap. Returns ``(tap, added)``; ``added`` is False when
    the tap is already known (a default or an earlier add)."""
    tid = normalize_tap(spec)
    existing = find_tap(tid)
    if existing is not None:
        return existing, False
    taps = _read_user_taps() + [tid]
    _write_json_atomic(_taps_path(), {"version": TAPS_VERSION, "taps": taps})
    return Tap(tid, "community"), True


def remove_tap(spec: str) -> bool:
    tid = normalize_tap(spec)
    if tid.lower() in {d.lower() for d, _ in DEFAULT_TAPS}:
        raise _err(f"{tid} is a default tap and cannot be removed")
    taps = _read_user_taps()
    keep = [t for t in taps if t.lower() != tid.lower()]
    if len(keep) == len(taps):
        return False
    _write_json_atomic(_taps_path(), {"version": TAPS_VERSION, "taps": keep})
    return True


# --- Search ------------------------------------------------------------------

@dataclass
class TapSkill:
    name: str
    description: str
    path: str  # path of the skill dir, relative to the REPO root
    tap: Tap


@dataclass
class TapListing:
    tap: Tap
    skills: List[TapSkill] = field(default_factory=list)
    error: Optional[str] = None
    stale: bool = False


class _NotFound(Exception):
    pass


def _fetch_json(url: str) -> Any:
    """GET ``url`` and parse JSON. Module-level test seam.

    Uses the install pipeline's https-only opener (no redirect off https). No
    credential or token header is sent. ``_NotFound`` on HTTP 404."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={
        "User-Agent": "polyrob-skill-hub",
        "Accept": "application/vnd.github+json, application/json",
    })
    try:
        with _si()._https_open(req, timeout=_HTTP_TIMEOUT_S) as r:  # nosec - operator-initiated
            data = r.read(_MAX_INDEX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _NotFound(url)
        raise
    if len(data) > _MAX_INDEX_BYTES:
        raise ValueError(f"response exceeds {_MAX_INDEX_BYTES} bytes")
    return json.loads(data.decode("utf-8", errors="replace"))


def _safe_rel_path(p: str) -> Optional[str]:
    parts = [x for x in str(p or "").strip().strip("/").split("/") if x]
    if not parts or any(x in (".", "..") or not _COMPONENT_RE.match(x) for x in parts):
        return None
    return "/".join(parts)


def _listing_from_index(tap: Tap, rows: Any) -> List[TapSkill]:
    if isinstance(rows, dict):
        rows = rows.get("skills")
    if not isinstance(rows, list):
        raise ValueError("skills/index.json is not a list")
    out: List[TapSkill] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "")
        rel = _safe_rel_path(r.get("path") or name)
        if not _SKILL_NAME_RE.match(name) or rel is None:
            logger.warning("skill hub: tap %s: ignoring index row %r", tap.id, r)
            continue
        repo_path = f"{tap.subdir}/{rel}" if tap.subdir else rel
        out.append(TapSkill(name, str(r.get("description") or ""), repo_path, tap))
    return out


def _listing_from_tree(tap: Tap, tree: Any) -> List[TapSkill]:
    entries = tree.get("tree") if isinstance(tree, dict) else None
    if not isinstance(entries, list):
        raise ValueError("unexpected git tree response")
    prefix = f"{tap.subdir}/" if tap.subdir else ""
    out: List[TapSkill] = []
    seen = set()
    for e in entries:
        path = str(e.get("path") or "") if isinstance(e, dict) else ""
        if not path.endswith("/SKILL.md") or not path.startswith(prefix):
            continue
        skill_dir = _safe_rel_path(path[: -len("/SKILL.md")])
        if skill_dir is None:
            continue
        name = skill_dir.rsplit("/", 1)[-1]
        if not _SKILL_NAME_RE.match(name) or skill_dir in seen:
            continue
        seen.add(skill_dir)
        out.append(TapSkill(name, "", skill_dir, tap))
    return sorted(out, key=lambda s: s.name)


def _fetch_listing(tap: Tap) -> List[TapSkill]:
    root = f"{tap.subdir}/" if tap.subdir else ""
    index_url = (f"https://raw.githubusercontent.com/{tap.owner}/{tap.repo}/HEAD/"
                 f"{root}skills/index.json")
    try:
        return _listing_from_index(tap, _fetch_json(index_url))
    except _NotFound:
        pass
    tree_url = (f"https://api.github.com/repos/{tap.owner}/{tap.repo}/git/trees/HEAD"
                f"?recursive=1")
    return _listing_from_tree(tap, _fetch_json(tree_url))


def _cache_path(tap: Tap) -> Path:
    return hub_dir() / "cache" / (tap.id.replace("/", "__") + ".json")


def _skills_to_rows(skills: List[TapSkill]) -> List[dict]:
    return [{"name": s.name, "description": s.description, "path": s.path} for s in skills]


def _rows_to_skills(tap: Tap, rows: List[dict]) -> List[TapSkill]:
    out = []
    for r in rows or []:
        rel = _safe_rel_path(r.get("path")) if isinstance(r, dict) else None
        name = str(r.get("name") or "") if isinstance(r, dict) else ""
        if rel and _SKILL_NAME_RE.match(name):
            out.append(TapSkill(name, str(r.get("description") or ""), rel, tap))
    return out


def tap_listing(tap: Tap, *, refresh: bool = False) -> TapListing:
    """The skills a tap offers — cached; a network failure is a named error on
    the listing (stale cache served when there is one), never an exception."""
    cp = _cache_path(tap)
    cached: Optional[dict] = None
    cache_error = ""
    if cp.is_file():
        try:
            import math
            cached = json.loads(cp.read_text(encoding="utf-8"))
            if not isinstance(cached, dict) or not isinstance(cached.get("skills"), list):
                raise ValueError("expected an object with a skills list")
            fetched_at = float(cached["fetched_at"])
            if not math.isfinite(fetched_at):
                raise ValueError("fetched_at is not finite")
            if len(_rows_to_skills(tap, cached["skills"])) != len(cached["skills"]):
                raise ValueError("invalid skill row")
        except Exception as exc:
            cache_error = f"cache unreadable ({type(exc).__name__}: {exc})"
            cached = None
    if cached and not refresh and time.time() - float(cached.get("fetched_at", 0)) < CACHE_TTL_S:
        return TapListing(tap, _rows_to_skills(tap, cached.get("skills")))
    try:
        skills = _fetch_listing(tap)
    except Exception as e:
        reason = "not found on GitHub" if isinstance(e, _NotFound) else f"{type(e).__name__}: {e}"
        if cached:
            return TapListing(tap, _rows_to_skills(tap, cached.get("skills")),
                              error=f"unreachable ({reason}); showing the cached list", stale=True)
        suffix = f"; {cache_error}" if cache_error else ""
        return TapListing(tap, [], error=f"unreachable ({reason}){suffix}")
    try:
        _write_json_atomic(cp, {"fetched_at": time.time(), "tap": tap.id,
                                "skills": _skills_to_rows(skills)})
    except OSError as e:
        logger.warning("skill hub: cannot write cache %s: %s", cp, e)
    return TapListing(tap, skills)


def search(query: str, *, refresh: bool = False) -> List[TapListing]:
    """Every tap's listing, filtered to rows whose name/description contains
    ``query`` (case-insensitive; an empty query keeps everything)."""
    q = (query or "").strip().lower()
    out = []
    for tap in list_taps():
        lst = tap_listing(tap, refresh=refresh)
        if q:
            lst.skills = [s for s in lst.skills
                          if q in s.name.lower() or q in s.description.lower()]
        out.append(lst)
    return out


# --- Install by name --------------------------------------------------------

def resolve_by_name(spec: str, *, refresh: bool = False) -> Optional[TapSkill]:
    """``<tap>/<skill>`` or a bare ``<skill>`` → the tap row, else ``None``
    (the spec is not a tap name and goes to ``dispatch_install`` unchanged).

    Refuses (``InstallError``) an ambiguous bare name, and a ``<tap>/<skill>``
    whose tap does not list that skill."""
    s = (spec or "").strip()
    if not s or "@" in s or s.startswith((".", "/", "~")) or "://" in s or Path(s).is_dir():
        return None
    if "/" in s:
        tap_id, _, name = s.rpartition("/")
        tap = find_tap(tap_id)
        if tap is None:
            return None
        lst = tap_listing(tap, refresh=refresh)
        hits = [x for x in lst.skills if x.name == name]
        if not hits:
            why = f" (tap {lst.error})" if lst.error else ""
            raise _err(f"tap {tap.id} lists no skill named {name!r}{why}. To install a path "
                       f"inside the repo directly, use the full git path owner/repo/<path>.")
        if len({hit.path for hit in hits}) > 1:
            paths = ", ".join(sorted({f"{tap.owner}/{tap.repo}/{hit.path}" for hit in hits}))
            raise _err(f"{s!r} is ambiguous within tap {tap.id}; install a full path: {paths}")
        return hits[0]
    if not _SKILL_NAME_RE.match(s):
        return None
    hits: List[TapSkill] = []
    errors: List[str] = []
    for lst in search("", refresh=refresh):
        hits += [x for x in lst.skills if x.name == s]
        if lst.error:
            errors.append(f"{lst.tap.id}: {lst.error}")
    if not hits:
        extra = ("; " + "; ".join(errors)) if errors else ""
        raise _err(f"no tap lists a skill named {s!r}{extra}")
    if len(hits) > 1:
        names = ", ".join(f"{h.tap.id}/{h.name}" for h in hits)
        raise _err(f"{s!r} is in more than one tap — install one of: {names}")
    return hits[0]


def install_spec(spec: str, *, user_id: str, trust: str = "prompt",
                 ref: Optional[str] = None):
    """The ONE CLI/REPL install entry: a tap name resolves to its git spec;
    everything goes through ``skill_install.dispatch_install``."""
    si = _si()
    hit = resolve_by_name(spec)
    if hit is None:
        return si.dispatch_install(spec, user_id=user_id, trust=trust, ref=ref)
    git_spec = f"{hit.tap.owner}/{hit.tap.repo}/{hit.path}"
    return si.dispatch_install(git_spec, user_id=user_id, trust=trust, ref=ref,
                               origin=si.InstallOrigin(tap=hit.tap.id, tier=hit.tap.tier))


# --- Trust matrix -----------------------------------------------------------

def trust_decision(*, source_kind: str, tier: Optional[str], verdict: str,
                   trust_flag: str = "prompt") -> str:
    """Proposal 067 §6 item 7. Returns ``approve`` | ``quarantine`` | ``refuse``.

    ``source_kind``: ``local`` (a folder) | ``tap`` (resolved by name through a
    tap) | ``remote`` (a git spec or URL given directly).
    ``verdict``: ``safe`` | ``caution`` | ``dangerous``. The install scanner is
    binary today — a clean scan is ``safe``, a hit is ``dangerous`` (the
    pipeline refuses it before this is asked). ``caution`` is NOT produced
    yet; the row exists so a graded scanner can plug in.

    | source                 | safe         | caution    | dangerous |
    |------------------------|--------------|------------|-----------|
    | local, --trust local   | approve      | quarantine | refuse    |
    | local, --trust prompt  | quarantine   | quarantine | refuse    |
    | official/trusted tap   | approve      | quarantine | refuse    |
    | community tap/URL/git  | quarantine   | quarantine | refuse    |

    There is no ``--force``: a dangerous verdict is always refused."""
    if verdict not in ("safe", "caution"):
        return "refuse"
    if verdict == "caution":
        return "quarantine"
    if source_kind == "local":
        return "approve" if trust_flag == "local" else "quarantine"
    if source_kind == "tap" and tier in AUTO_APPROVE_TIERS:
        return "approve"
    return "quarantine"


def source_trust(source: str, tier: Optional[str]) -> str:
    """The ``trust`` recorded in the lock for an install."""
    if tier:
        return tier
    return "local" if source == "local" else "community"


# --- Lock -------------------------------------------------------------------

_SKIP_IN_DIGEST = frozenset({".install-meta.json"})


def tree_digest(folder: Path) -> Tuple[Dict[str, str], str]:
    """``({relpath: sha256}, content_sha256)`` over every regular file in
    ``folder`` (the install metadata file excluded). The combined hash is the
    sha256 of the sorted ``relpath\\0filehash\\n`` lines."""
    files: Dict[str, str] = {}
    for p in sorted(Path(folder).rglob("*")):
        if not p.is_file() or p.is_symlink() or p.name in _SKIP_IN_DIGEST:
            continue
        rel = p.relative_to(folder).as_posix()
        files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{files[rel]}\n".encode("utf-8"))
    return files, h.hexdigest()


def _lock_path() -> Path:
    return hub_dir() / "lock.json"


def validate_lock_entry(name: str, entry: Any, root: Optional[Path] = None) -> Optional[str]:
    """``None`` when the entry is safe to use, else the reason it is not.

    The name must pass the skill writer's id rule, and ``install_path`` must
    resolve to ``<root>/user_<uid>/<name>`` — never outside the user skill
    scope (a poisoned lock must not point an rmtree/copy anywhere else)."""
    if not isinstance(entry, dict):
        return "entry is not an object"
    try:
        ok, errs = _si()._skill_manager().validate_skill_id(name)
    except Exception as e:
        return f"cannot validate name: {e}"
    if not ok:
        return f"bad name {name!r}: {'; '.join(errs)}"
    ip = entry.get("install_path")
    if not isinstance(ip, str) or not ip:
        return "missing install_path"
    root = Path(root or skills_root()).resolve()
    try:
        real = Path(ip).resolve()
    except (OSError, RuntimeError) as e:
        return f"install_path unresolvable: {e}"
    if root not in real.parents:
        return f"install_path {ip!r} escapes the user skill scope {str(root)!r}"
    if real.name != name or real.parent.parent != root or not real.parent.name.startswith("user_"):
        return f"install_path {ip!r} is not <skills>/user_<uid>/{name}"
    uid = entry.get("user_id")
    if uid is not None and real.parent.name != f"user_{str(uid).strip()}":
        return "user_id disagrees with install_path tenant"
    return None


def _lock_key(name: str, entry: dict) -> str:
    return f"{Path(entry['install_path']).resolve().parent.name}/{name}"


def _read_lock_raw() -> Dict[str, Any]:
    p = _lock_path()
    if not p.is_file():
        return {"version": LOCK_VERSION, "installed": {}}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("installed"), dict):
        raise ValueError("lock.json has no 'installed' object")
    version = data.get("version", 1)
    if version not in (1, LOCK_VERSION):
        raise ValueError(f"unsupported skill lock version {version!r}")
    if version == 1:
        migrated = {}
        for name, entry in data["installed"].items():
            reason = validate_lock_entry(name, entry)
            if reason:
                logger.warning("skill lock: ignoring legacy entry %r: %s", name, reason)
            else:
                migrated[_lock_key(name, entry)] = entry
        data = {**data, "version": LOCK_VERSION, "installed": migrated}
    return data


def read_lock(*, user_id: str) -> Dict[str, dict]:
    """The requested tenant's VALID lock entries. A bad entry is logged and left out (never
    followed). An unreadable lock file raises a named ``InstallError``."""
    try:
        data = _read_lock_raw()
    except Exception as e:
        raise _err(f"cannot read {_lock_path()}: {e}")
    out: Dict[str, dict] = {}
    tenant = _si()._skill_manager()._user_root(user_id).name
    for key, entry in data["installed"].items():
        name = key.rsplit("/", 1)[-1]
        reason = validate_lock_entry(name, entry)
        if reason is None and key != _lock_key(name, entry):
            reason = "lock key disagrees with install_path"
        if reason:
            logger.warning("skill lock: ignoring entry %r: %s", name, reason)
            continue
        if key == f"{tenant}/{name}":
            out[name] = entry
    return out


def write_lock_entry(name: str, entry: dict) -> bool:
    """Validate, then write one entry. ``False`` (logged) when the entry is
    invalid or the existing lock cannot be read (it is not overwritten)."""
    reason = validate_lock_entry(name, entry)
    if reason:
        logger.warning("skill lock: refusing to record %r: %s", name, reason)
        return False
    try:
        data = _read_lock_raw()
    except Exception as e:
        logger.warning("skill lock: %s unreadable, not overwritten: %s", _lock_path(), e)
        return False
    data["version"] = LOCK_VERSION
    data["installed"][_lock_key(name, entry)] = entry
    _write_json_atomic(_lock_path(), data)
    return True


def remove_lock_entry(name: str, *, user_id: str) -> bool:
    key = f"{_si()._skill_manager()._user_root(user_id).name}/{name}"
    try:
        data = _read_lock_raw()
    except Exception as e:
        logger.warning("skill lock: %s unreadable, not changed: %s", _lock_path(), e)
        return False
    if key not in data["installed"]:
        return False
    del data["installed"][key]
    _write_json_atomic(_lock_path(), data)
    return True


def record_approved(name: str, *, user_id: str, meta: Dict[str, Any],
                    files: Dict[str, str], content_sha256: str, install_path: Path) -> bool:
    """Called by ``skill_install._approve`` after a successful promote."""
    source = str(meta.get("source") or "local")
    tier = meta.get("tier")
    entry = {
        "source": source,
        "tap": meta.get("tap"),
        "ref_sha": meta.get("resolved_sha"),
        "trust": source_trust(source, tier),
        "scan_verdict": "safe",
        "content_sha256": content_sha256,
        "files": files,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "install_path": str(install_path),
        "user_id": user_id,
    }
    return write_lock_entry(name, entry)


# --- Update -----------------------------------------------------------------

@dataclass
class UpdateReport:
    name: str
    status: str  # up-to-date | approved | quarantined | refused | skipped | error
    detail: str = ""
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    old_sha: str = ""
    new_sha: str = ""


def _discard_staged(name: str, user_id: str) -> None:
    mgr = _si()._skill_manager()
    pending_root = (mgr._user_root(user_id) / ".pending").resolve()
    staged = (pending_root / name).resolve()
    if staged.parent == pending_root and staged.is_dir():
        shutil.rmtree(staged, ignore_errors=True)


def _update_one(name: str, entry: dict, *, user_id: str) -> UpdateReport:
    si = _si()
    source = str(entry.get("source") or "")
    if source.startswith("git:"):
        spec = source[4:]
    elif source.startswith("url:"):
        spec = source[4:]
    else:
        return UpdateReport(name, "skipped",
                            "installed from a local folder — re-run skill install <folder>")
    tap_id = entry.get("tap")
    tap = find_tap(tap_id) if tap_id else None
    if tap_id and tap is None:
        tier, kind = None, "remote"  # the tap was removed: community rules
    else:
        tier, kind = (tap.tier, "tap") if tap else (None, "remote")
    origin = si.InstallOrigin(tap=tap.id if tap else None, tier=tier, stage_only=True)
    try:
        res = si.dispatch_install(spec, user_id=user_id, trust="prompt", origin=origin)
    except Exception as e:  # a scan hit is an InstallError: refuse, active copy untouched
        return UpdateReport(name, "refused", str(e))
    if res.name != name:
        _discard_staged(res.name, user_id)
        return UpdateReport(name, "error", f"upstream now names the skill {res.name!r}")
    new_files, new_sha = tree_digest(res.staged_path)
    old_files = entry.get("files") if isinstance(entry.get("files"), dict) else {}
    old_sha = str(entry.get("content_sha256") or "")
    rep = UpdateReport(name, "", old_sha=old_sha, new_sha=new_sha)
    if new_sha == old_sha:
        _discard_staged(name, user_id)
        rep.status = "up-to-date"
        return rep
    rep.added = sorted(set(new_files) - set(old_files))
    rep.removed = sorted(set(old_files) - set(new_files))
    rep.changed = sorted(f for f in set(new_files) & set(old_files) if new_files[f] != old_files[f])
    decision = trust_decision(source_kind=kind, tier=tier, verdict="safe")
    if decision == "approve":
        si._approve(name, user_id=user_id, source=source)
        rep.status = "approved"
    else:
        rep.status = "quarantined"
        rep.detail = f"run polyrob skill approve {name} to activate the new version"
    return rep


def update_skills(name: Optional[str], *, user_id: str) -> List[UpdateReport]:
    lock = read_lock(user_id=user_id)
    if name:
        if name not in lock:
            return [UpdateReport(name, "skipped",
                                 "not in the skill lock (installed before the lock, or not "
                                 "installed) — install it again to track it")]
        return [_update_one(name, lock[name], user_id=user_id)]
    return [_update_one(n, lock[n], user_id=user_id) for n in sorted(lock)]


def format_update(rep: UpdateReport) -> List[str]:
    lines = [f"{rep.name}: {rep.status}" + (f" — {rep.detail}" if rep.detail else "")]
    if rep.status in ("approved", "quarantined"):
        lines.append(f"  content {rep.old_sha[:12] or '?'} -> {rep.new_sha[:12]}; "
                     f"{len(rep.changed)} changed, {len(rep.added)} added, "
                     f"{len(rep.removed)} removed")
        for label, items in (("changed", rep.changed), ("added", rep.added),
                             ("removed", rep.removed)):
            for f in items:
                lines.append(f"    {label}: {f}")
    return lines


def format_listing(lst: TapListing) -> List[str]:
    head = f"{lst.tap.id} [{lst.tap.tier}]"
    if lst.error and not lst.skills:
        return [f"{head}: {lst.error}"]
    lines = [f"{head}: {len(lst.skills)} match(es)" + (f" — {lst.error}" if lst.error else "")]
    for s in lst.skills:
        desc = " ".join(s.description.split())[:80]
        lines.append(f"  {lst.tap.id}/{s.name}" + (f" — {desc}" if desc else ""))
    return lines


# --- CLI -----------------------------------------------------------------------

def register_hub_commands(group: click.Group) -> None:
    """Attach ``tap``/``search``/``update`` to the ``skill`` group."""

    @group.group("tap")
    def tap_group():
        """Manage skill taps (GitHub repos that publish skills)."""
        # The parent `skill` callback already ran it; memoized, so this is a no-op
        # there and keeps the group correct if it is ever mounted elsewhere.
        from cli.commands._bootstrap import ensure_env_loaded
        ensure_env_loaded()

    @tap_group.command("list")
    def tap_list():
        """List the taps and their trust tier."""
        for t in list_taps():
            click.echo(f"{t.id:<36} {t.tier:<10}" + (" (default)" if t.default else ""))

    @tap_group.command("add")
    @click.argument("spec")
    def tap_add(spec: str):
        """Add a tap: owner/repo[/subdir]. An added tap has community trust."""
        tap, added = add_tap(spec)
        if added:
            click.echo(f"[polyrob] added tap {tap.id} (community — installs are quarantined).")
        else:
            click.echo(f"[polyrob] tap {tap.id} is already known ({tap.tier}).")

    @tap_group.command("remove")
    @click.argument("spec")
    def tap_remove(spec: str):
        """Remove a tap you added (default taps cannot be removed)."""
        if remove_tap(spec):
            click.echo(f"[polyrob] removed tap {normalize_tap(spec)}.")
        else:
            raise _err(f"no added tap {spec!r}")

    @group.command("search")
    @click.argument("query", required=False, default="")
    @click.option("--refresh", is_flag=True, help="Ignore the cache and fetch every tap again.")
    def skill_search(query: str, refresh: bool):
        """Search the taps for skills by name or description."""
        for lst in search(query, refresh=refresh):
            for line in format_listing(lst):
                click.echo(line)

    @group.command("update")
    @click.argument("name", required=False)
    @click.option("--user", "user_id", default=None, help="Tenant user_id (default: local owner id).")
    def skill_update(name: Optional[str], user_id: Optional[str]):
        """Re-fetch installed skills from their source and re-apply the trust rules."""
        uid = user_id or _si()._default_user()
        reports = update_skills(name, user_id=uid)
        if not reports:
            click.echo("No skills in the lock.")
        for rep in reports:
            for line in format_update(rep):
                click.echo(line)
