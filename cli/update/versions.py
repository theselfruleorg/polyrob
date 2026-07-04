"""Version resolution + comparison for `polyrob update`.

Installed version comes from the app SSOT (``core.version.get_version``). "Latest"
comes from PyPI (pip/pipx/docker installs) or GitHub tags (git installs). Network
fetches are injected so the logic is unit-testable offline.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

_SEMVER_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:[-+.].*)?\s*$")

# Default release home. Overridable so a fork / private mirror / renamed repo works
# without a code change (§2.2 — the repo name is not hardcoded to a soon-gone value).
_DEFAULT_REPO = "theselfruleorg/polyrob"
_DEFAULT_PYPI = "polyrob"


def parse_semver(text: str) -> Optional[Tuple[int, int, int]]:
    """Parse ``X.Y.Z`` (optional ``v`` prefix / pre-release suffix) → tuple, else None."""
    m = _SEMVER_RE.match(text or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def is_prerelease(text: str) -> bool:
    return bool(re.search(r"[-+.](?:a|b|rc|alpha|beta|dev|pre)", (text or "").lower()))


def compare(a: str, b: str) -> int:
    """-1 if a<b, 0 if equal, 1 if a>b (by semver; unparseable sorts lowest)."""
    pa, pb = parse_semver(a), parse_semver(b)
    if pa is None and pb is None:
        return 0
    if pa is None:
        return -1
    if pb is None:
        return 1
    return (pa > pb) - (pa < pb)


def select_latest(versions: Iterable[str], *, include_prerelease: bool = False) -> Optional[str]:
    """Highest semver from an iterable, filtering pre-releases unless asked."""
    best: Optional[str] = None
    for v in versions:
        if parse_semver(v) is None:
            continue
        if not include_prerelease and is_prerelease(v):
            continue
        if best is None or compare(v, best) > 0:
            best = v
    return best


@dataclass(frozen=True)
class UpdateStatus:
    current: str
    latest: Optional[str]
    channel: str
    error: Optional[str] = None       # None | offline | not_found | no_releases | http_error | parse_error
    source_ref: str = ""              # the repo/package that was queried

    @property
    def update_available(self) -> bool:
        return self.latest is not None and compare(self.latest, self.current) > 0

    @property
    def is_downgrade(self) -> bool:
        return self.latest is not None and compare(self.latest, self.current) < 0

    @property
    def human_note(self) -> str:
        """Informative one-liner for the failure/latest state (never a bare guess)."""
        ref = self.source_ref or "the release channel"
        if self.error == "offline":
            return "could not check for updates (offline or network error)"
        if self.error == "not_found":
            return (f"could not find {ref} — the repository/package may be private, "
                    "renamed, or not published yet")
        if self.error == "no_releases":
            return f"no releases published yet for {ref}"
        if self.error in ("http_error", "parse_error"):
            return f"could not read the release list for {ref} ({self.error})"
        if self.latest is None:
            return "could not determine the latest version"
        return ""

    def as_dict(self) -> dict:
        return {
            "current": self.current,
            "latest": self.latest,
            "channel": self.channel,
            "update_available": self.update_available,
            "error": self.error,
            "source_ref": self.source_ref,
        }


def installed_version() -> str:
    from core.version import get_version

    return get_version()


# --- repo / package resolution -------------------------------------------------

def _repo_from_metadata() -> Optional[str]:
    """Derive ``owner/name`` from the installed dist's Project-URL, else None."""
    try:
        from importlib.metadata import metadata

        md = metadata("polyrob")
        urls = md.get_all("Project-URL") or []
        # Also honour Home-page (older metadata).
        candidates = [u.split(",", 1)[-1].strip() for u in urls]
        hp = md.get("Home-page")
        if hp:
            candidates.append(hp)
        for url in candidates:
            m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s#]+?)(?:\.git)?/?$", url)
            if m:
                return f"{m.group(1)}/{m.group(2)}"
    except Exception:
        pass
    return None


def resolve_repo() -> str:
    """GitHub ``owner/name``: env override → dist metadata → default."""
    env = os.getenv("POLYROB_UPDATE_REPO")
    if env and "/" in env:
        return env.strip()
    return _repo_from_metadata() or _DEFAULT_REPO


def resolve_pypi_package() -> str:
    """PyPI project name: env override → default."""
    return (os.getenv("POLYROB_UPDATE_PYPI") or _DEFAULT_PYPI).strip()


def _classify_fetch_error(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code == 404:
        return "not_found"
    if code is not None:
        return "http_error"
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "parse_error"
    # URLError / socket.timeout / OSError / anything network-ish.
    return "offline"


# --- latest-version fetchers (network injected via `fetch`) --------------------

def pypi_versions(fetch: Callable[[str], str], package: str = "polyrob") -> List[str]:
    """All release versions from the PyPI JSON API. ``fetch(url) -> body``."""
    body = fetch(f"https://pypi.org/pypi/{package}/json")
    data = json.loads(body)
    return list(data.get("releases", {}).keys()) or [data.get("info", {}).get("version", "")]


def github_tag_versions(fetch: Callable[[str], str],
                       repo: str = "theselfruleorg/polyrob") -> List[str]:
    """Release tag names from the GitHub Releases API."""
    body = fetch(f"https://api.github.com/repos/{repo}/releases")
    data = json.loads(body)
    return [rel.get("tag_name", "") for rel in data if isinstance(rel, dict)]


def resolve_status(
    *,
    channel: str,
    fetch: Callable[[str], str],
    source: str = "pypi",
    current: Optional[str] = None,
    repo: Optional[str] = None,
    package: Optional[str] = None,
) -> UpdateStatus:
    """Compute current-vs-latest. ``source`` = 'pypi' | 'github'. Fail-soft, but the
    failure is *classified* (offline / not_found / no_releases / …) so the caller can
    report an informative message instead of a bare 'could not check' (§2.2)."""
    cur = current or installed_version()
    include_pre = channel in {"pre", "beta"}
    repo = repo or resolve_repo()
    package = package or resolve_pypi_package()
    ref = package if source == "pypi" else repo

    latest: Optional[str] = None
    error: Optional[str] = None
    try:
        raw = (pypi_versions(fetch, package) if source == "pypi"
               else github_tag_versions(fetch, repo))
        latest = select_latest(raw, include_prerelease=include_pre)
        if latest is None:
            error = "no_releases"
    except Exception as exc:
        latest = None
        error = _classify_fetch_error(exc)
    return UpdateStatus(current=cur, latest=latest, channel=channel,
                        error=error, source_ref=ref)
