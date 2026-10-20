"""The curated pack index and the kill list (067 P7, proposal §7) — local
files, no network.

- First-party rows are BUILT AT RUNTIME (W11): one row per ``polyrob.packs``
  entry point the ``polyrob`` distribution declares (the source checkout's
  ``pyproject.toml`` first, like ``core.version``; else the installed
  metadata), with the pack's own ``pack.toml`` (summary, capabilities) and the
  core version. They are the ONE list of core-reviewed identities the loader
  grants the custody exemption (``core.packs.loader._FIRST_PARTY``):
  distribution AND entry point must match. A release bump edits nothing here.
- ``index.json`` — the THIRD-PARTY rows ``polyrob pack install <id>`` resolves:
  ``id, dist, version, sha256, tier, summary, capabilities, requires_core,
  homepage, entry_point``. Human-merged, reviewed rows with a pinned version and
  wheel hash. A first-party row in the file is ignored (the file cannot grant
  that identity), and so is a third-party row that reuses a first-party id.
- ``removed.json`` — the kill list: ``id, versions (PEP 440 specifier), reason,
  date`` (optional ``dist``). Enforced by the loader at phase 1 AND phase 2,
  ``polyrob pack enable``, ``polyrob pack install`` and (via :func:`killed`)
  the updater.

Both files are package data of this package (one copy each). No imports above core.
"""
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
INDEX_FILE = _HERE / "index.json"
REMOVED_FILE = _HERE / "removed.json"

ROW_KEYS = ("id", "dist", "version", "sha256", "tier", "summary", "capabilities",
            "requires_core", "homepage", "entry_point")
REMOVED_KEYS = frozenset({"id", "dist", "versions", "reason", "date"})

#: 067 (one install, owner decision 2026-09-25): the first-party packs ship INSIDE
#: the ``polyrob`` distribution. These are the separate distribution names they
#: had before — never published, so a package index may hand them to anyone.
#: THE one list (tests/test_retired_pack_dist_names_ratchet.py): a third-party
#: install may not take one; the loader never loads one and names the uninstall;
#: the deploy (``scripts/deploy_dependency_cleanup.py --retire-pack-dists``) and
#: ``polyrob update`` uninstall a leftover one.
RETIRED_DISTS = frozenset({"polyrob-discovery", "polyrob-markets", "polyrob-x"})


class PackIndexError(ValueError):
    """A malformed index or kill-list row."""


def _canon(name: str) -> str:
    from packaging.utils import canonicalize_name
    return canonicalize_name(name or "")


def _read(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise PackIndexError(f"{path.name} must be a JSON list")
    return data


def validate_row(row: dict) -> dict:
    """Raise :class:`PackIndexError` unless *row* is a well-formed index row."""
    import re
    if not isinstance(row, dict) or set(row) - set(ROW_KEYS):
        raise PackIndexError(f"index row has unknown keys: {row!r}")
    for key in ("id", "dist", "version", "tier", "entry_point"):
        if not isinstance(row.get(key), str) or not row[key]:
            raise PackIndexError(f"index row {row.get('id')!r}: {key} is required")
    if not re.match(r"^[a-z][a-z0-9_]{0,31}$", row["id"]):
        raise PackIndexError(f"index row id {row['id']!r} is not a pack id")
    # dist and version are written into a pip requirements line (`dist==version
    # --hash=...`): only a PEP 508 project name and a plain PEP 440 version may
    # pass, never whitespace, an option or a URL.
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?", row["dist"]):
        raise PackIndexError(f"index row {row['id']!r}: dist {row['dist']!r} is not a project name")
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+!]{0,63}", row["version"]):
        raise PackIndexError(f"index row {row['id']!r}: version {row['version']!r} is not a version")
    from packaging.version import InvalidVersion, Version
    try:
        Version(row["version"])
    except InvalidVersion:
        raise PackIndexError(f"index row {row['id']!r}: version {row['version']!r} is not PEP 440")
    if row["tier"] not in ("first-party", "third-party"):
        raise PackIndexError(f"index row {row['id']!r}: tier {row['tier']!r}")
    sha = row.get("sha256") or ""
    if sha and not re.match(r"^[0-9a-f]{64}$", sha):
        raise PackIndexError(f"index row {row['id']!r}: sha256 must be 64 hex")
    if row["tier"] == "third-party" and not sha:
        raise PackIndexError(f"index row {row['id']!r}: a third-party row pins its wheel sha256")
    if not isinstance(row.get("capabilities", []), list):
        raise PackIndexError(f"index row {row['id']!r}: capabilities must be a list")
    return row


def validate_removed(row: dict) -> dict:
    """Raise :class:`PackIndexError` unless *row* is a well-formed kill-list row."""
    import re
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    if not isinstance(row, dict) or set(row) - REMOVED_KEYS:
        raise PackIndexError(f"removed row has unknown keys: {row!r}")
    for key in ("id", "versions", "reason", "date"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise PackIndexError(f"removed row {row.get('id')!r}: {key} is required")
    try:
        SpecifierSet(row["versions"])
    except InvalidSpecifier as exc:
        raise PackIndexError(f"removed row {row['id']!r}: versions {row['versions']!r}: {exc}")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", row["date"]):
        raise PackIndexError(f"removed row {row['id']!r}: date must be YYYY-MM-DD")
    return row


GROUP = "polyrob.packs"
DIST = "polyrob"
HOMEPAGE = "https://github.com/theselfruleorg/polyrob/tree/main/packs/{id}"


def _source_entry_points() -> Optional[Dict[str, str]]:
    """The ``polyrob.packs`` entry points of the source checkout's
    ``pyproject.toml`` (``core/packs/`` -> repo root), or None when this is not
    a polyrob source tree. First, like ``core.version``: a stale egg-info or
    editable metadata must not shadow the code that actually runs."""
    try:
        import tomllib
        path = _HERE.parents[1] / "pyproject.toml"
        if not path.is_file():
            return None
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
        if str(project.get("name", "")).strip().lower() != DIST:
            return None
        eps = (project.get("entry-points") or {}).get(GROUP) or {}
        return {str(k): str(v) for k, v in eps.items()}
    except Exception:  # noqa: BLE001 — fall through to the installed metadata
        return None


def first_party_entry_points() -> Dict[str, str]:
    """``pack id -> entry point`` of every pack shipped INSIDE ``polyrob``."""
    eps = _source_entry_points()
    if eps is not None:
        return eps
    try:
        from importlib.metadata import distribution
        dist = distribution(DIST)
        return {ep.name: ep.value for ep in dist.entry_points if ep.group == GROUP}
    except Exception:  # noqa: BLE001 — no metadata: no first-party identity (fail closed)
        return {}


def _pack_toml(entry_point: str) -> dict:
    """The pack's ``pack.toml`` (found WITHOUT importing the pack), or {}."""
    try:
        import importlib.util
        import tomllib
        spec = importlib.util.find_spec(entry_point.partition(":")[0].split(".", 1)[0])
        for loc in (spec.submodule_search_locations or []) if spec else []:
            path = Path(loc) / "pack.toml"
            if path.is_file():
                return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — the row stays, without summary/capabilities
        pass
    return {}


def first_party_tool_ids() -> frozenset:
    """Every ``[tools.<id>]`` a first-party pack declares, read WITHOUT importing it
    (a bare CLI process never loads packs, so the core capability table lacks them)."""
    ids = set()
    for entry_point in first_party_entry_points().values():
        ids.update((_pack_toml(entry_point).get("tools") or {}).keys())
    return frozenset(ids)


def first_party_rows() -> List[dict]:
    """One validated row per first-party entry point, built at runtime."""
    from core.version import get_version
    version = get_version()
    out = []
    for pack_id, entry_point in sorted(first_party_entry_points().items()):
        manifest = _pack_toml(entry_point)
        try:
            out.append(validate_row({
                "id": pack_id, "dist": DIST, "version": version, "sha256": "",
                "tier": "first-party", "summary": str(manifest.get("summary", "")),
                "capabilities": sorted(manifest.get("capabilities", []) or []),
                "requires_core": str(manifest.get("requires_core") or f"=={version}"),
                "homepage": str(manifest.get("homepage") or HOMEPAGE.format(id=pack_id)),
                "entry_point": entry_point,
            }))
        except PackIndexError:
            continue
    return out


def third_party_rows() -> List[dict]:
    """The reviewed third-party rows of ``index.json``. An unreadable file reads
    as empty; a first-party row in it, or one reusing a first-party id, is ignored."""
    first = set(first_party_entry_points())
    out = []
    try:
        data = _read(INDEX_FILE)
    except (OSError, ValueError):
        return []
    for raw in data:
        try:
            row = validate_row(raw)
        except PackIndexError:
            continue
        if row["tier"] == "third-party" and row["id"] not in first:
            out.append(row)
    return out


def rows() -> List[dict]:
    """Every index row (validated): the runtime first-party rows, then the
    reviewed third-party rows. With no polyrob metadata and no source tree there
    is no first-party row, and :func:`first_party_identities` grants no
    exemption (fail closed)."""
    return sorted(first_party_rows() + third_party_rows(), key=lambda r: r["id"])


def find(pack_id: str) -> Optional[dict]:
    return next((r for r in rows() if r["id"] == pack_id), None)


def search(query: str = "") -> List[dict]:
    """Rows whose id, dist, summary or capabilities contain *query* (case-folded)."""
    q = (query or "").strip().lower()
    out = []
    for r in rows():
        hay = " ".join([r["id"], r["dist"], r.get("summary", ""),
                        " ".join(r.get("capabilities", []))]).lower()
        if q in hay:
            out.append(r)
    return out


def retired_dist(name: str) -> bool:
    """True when *name* is one of :data:`RETIRED_DISTS` (canonicalized)."""
    return _canon(name) in RETIRED_DISTS


def first_party_identities() -> Dict[str, Tuple[str, str]]:
    """``pack id -> (canonical distribution, entry point)`` of every
    first-party index row: the core-reviewed identities."""
    return {pack_id: (_canon(DIST), entry_point)
            for pack_id, entry_point in first_party_entry_points().items()}


def removed_rows() -> List[dict]:
    """The kill list. ⚠️ An unreadable kill list RAISES: a list that could not
    be read is not an empty one."""
    return [validate_removed(r) for r in _read(REMOVED_FILE)]


def killed(name: str, version: Optional[str]) -> Optional[str]:
    """Why pack/distribution *name* at *version* is on the kill list, or None.

    *name* matches a row's ``id`` or its ``dist`` (canonicalized). A None or
    unparsable *version* matches any row for that name (fail closed). An
    unreadable kill list answers with that reason rather than None."""
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version
    try:
        entries = removed_rows()
    except (OSError, ValueError) as exc:
        return f"the pack kill list is unreadable ({type(exc).__name__}: {exc})"
    want = _canon(name)
    for row in entries:
        names = {_canon(row["id"])} | ({_canon(row["dist"])} if row.get("dist") else set())
        if want not in names:
            continue
        try:
            hit = version is None or Version(version) in SpecifierSet(row["versions"],
                                                                     prereleases=True)
        except InvalidVersion:
            hit = True
        if hit:
            return (f"on the pack kill list ({row['id']} {row['versions']}, {row['date']}): "
                    f"{row['reason']}")
    return None


__all__ = ["INDEX_FILE", "REMOVED_FILE", "RETIRED_DISTS", "ROW_KEYS", "retired_dist", "find",
           "first_party_entry_points", "first_party_identities", "first_party_rows",
           "third_party_rows",
           "killed", "removed_rows", "rows", "search", "validate_removed", "validate_row"]
