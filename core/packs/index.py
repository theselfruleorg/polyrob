"""The curated pack index and the kill list (067 P7, proposal §7) — local
files, no network.

- ``index.json`` — every pack ``polyrob pack install <id>`` resolves. Rows:
  ``id, dist, version, sha256, tier, summary, capabilities, requires_core,
  homepage, entry_point``. First-party rows are GENERATED from the core pyproject
  (``scripts/gen_pack_index.py``); third-party rows are human-merged, reviewed
  rows with a pinned version and wheel hash. The first-party rows are also the
  ONE list of core-reviewed identities the loader grants the custody exemption
  (``core.packs.loader._FIRST_PARTY``): distribution AND entry point must match.
- ``removed.json`` — the kill list: ``id, versions (PEP 440 specifier), reason,
  date`` (optional ``dist``). Enforced by the loader at phase 1 AND phase 2,
  ``polyrob pack enable``, ``polyrob pack install`` and (via :func:`killed`)
  the updater.

The repo copies live in ``packs/``; the generator (``scripts/gen_pack_index.py``) mirrors them byte-identical
into this package so an installed core reads them (``*.json`` is package data).
No imports above core.
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


def rows() -> List[dict]:
    """The index rows (validated). An unreadable index reads as empty here;
    :func:`first_party_identities` then grants no exemption (fail closed)."""
    try:
        return [validate_row(r) for r in _read(INDEX_FILE)]
    except (OSError, ValueError):
        return []


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
    return {r["id"]: (_canon(r["dist"]), r["entry_point"])
            for r in rows() if r["tier"] == "first-party"}


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


__all__ = ["INDEX_FILE", "REMOVED_FILE", "RETIRED_DISTS", "ROW_KEYS", "retired_dist", "find", "first_party_identities",
           "killed", "removed_rows", "rows", "search", "validate_removed", "validate_row"]
