"""core/profiles.py — named-profile resolution + activation (multi-instance W2).

A PROFILE is a fully independent POLYROB home directory: its own ``.env``,
``cli.json``, ``mcp.json``, ``auth.json``, ``characters/``, ``skills/`` and a
``data/`` subtree (identity docs, memory.db, goals.db, cron.db, sessions).
Profiles live under ``<base home>/profiles/<name>/``; selecting one simply
points the two existing path seams at it:

    POLYROB_HOME     = <profile>            (config home — core/paths.py)
    POLYROB_DATA_DIR = <profile>/data       (data home — core/runtime_paths.py)

Selection order (strongest first):

    1. ``--profile``/``-P`` CLI flag        -> OVERWRITES the homes
    2. ``POLYROB_PROFILE`` env               -> OVERWRITES the homes
    3. project pin ``./.polyrob/profile``    -> DEFERS to an already-set env
       (nearest file walking cwd up to the git root)
    4. sticky ``<base home>/active_profile`` -> DEFERS to an already-set env
    5. none                                  -> legacy/project mode (byte-identical)

Why the OVERWRITE/DEFER split: an explicit ``-P`` is the strongest statement of
intent (wrapper binaries work by re-invoking with it), so it must beat a shell
that exports ``POLYROB_HOME`` for another profile. A pin or sticky file is a
weak ambient signal and must never override an operator who set the environment
on purpose (this keeps prod's explicit ``POLYROB_DATA_DIR=/var/lib/polyrob``
safe). A deferral is never silent — a one-shot mismatch warning names the
intended vs actual home.

``POLYROB_PROFILE`` present-but-EMPTY is an explicit "no profile": it disables
the pin/sticky tiers (the escape hatch back to legacy mode).

Timing: :func:`activate_profile` must run at the CLI entry BEFORE any
``load_env`` call — flags freeze at import and ``load_env`` refreezes them
once. The click group callback is early enough; never move this behind an
import of ``agents.*`` or ``tools.*``.

Dependency-light on purpose, and NO ``from __future__ import annotations``
(same discipline as ``core/paths.py``).
"""

import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from core.paths import polyrob_home

logger = logging.getLogger(__name__)

# Reject-never-sanitize (mirrors core.instance.is_safe_tenant_id): silently
# stripping characters can collapse two distinct names (``a/b`` and ``ab``)
# into the same directory — a cross-profile collision/leak.
PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

#: Names that can never be profiles (would collide with base-home content).
_RESERVED_NAMES = frozenset({"default", "profiles", "data"})

_STICKY_FILE_NAME = "active_profile"
_PIN_REL_PATH = Path(".polyrob") / "profile"

#: Process-wide marker: profile resolution RAN in this process (whatever the
#: outcome). Suppresses the wrong-profile fallback warning for entrypoints that
#: resolve properly; a subprocess spawned with a minimal env loses it together
#: with POLYROB_HOME, which is exactly when the warning should fire.
_RESOLVED_MARKER = "POLYROB_PROFILE_RESOLVED"

_warned_mismatch = False
_warned_fallback = False


class ProfileError(ValueError):
    """Base class for profile-selection errors."""


class InvalidProfileNameError(ProfileError):
    def __init__(self, name):
        super().__init__(
            f"invalid profile name {name!r}: use letters, digits, '-' and '_' "
            f"only (max 64 chars); path-shaped names are rejected, never rewritten"
        )
        self.name = name


class ProfileNotFoundError(ProfileError):
    def __init__(self, name, home):
        super().__init__(
            f"profile {name!r} does not exist (looked in {home}). "
            f"Create it with: polyrob profile create {name}"
        )
        self.name = name
        self.home = home


@dataclass(frozen=True)
class ProfileSelection:
    """One resolved profile selection: name, home dir, and which tier chose it."""
    name: str
    home: Path
    source: str  # "flag" | "env" | "project_pin" | "sticky"


@dataclass(frozen=True)
class ProfileInfo:
    """One profile as listed from disk (metadata read fail-open)."""
    name: str
    home: Path
    display_name: str = ""
    description: str = ""


def is_safe_profile_name(name: Optional[str]) -> bool:
    """True if *name* is a path-safe profile name. Reject, never sanitize."""
    if not name:
        return False
    if name in _RESERVED_NAMES:
        return False
    return bool(PROFILE_ID_RE.fullmatch(str(name)))


def base_home() -> Path:
    """The BASE home holding the profile registry + the sticky file.

    Once a profile is active, ``polyrob_home()`` points INSIDE the profile, so
    the registry location is pinned via ``POLYROB_PROFILES_ROOT`` (set by
    :func:`apply_profile_env` before it moves ``POLYROB_HOME``). Outside a
    profile, the base home IS ``polyrob_home()`` (tests isolate via
    ``POLYROB_HOME`` and get an isolated registry for free).
    """
    root = (os.environ.get("POLYROB_PROFILES_ROOT") or "").strip()
    if root:
        return Path(root).parent
    return polyrob_home()


def profiles_root() -> Path:
    """Directory holding all named profiles."""
    root = (os.environ.get("POLYROB_PROFILES_ROOT") or "").strip()
    if root:
        return Path(root)
    return polyrob_home() / "profiles"


def profile_dir(name: str) -> Path:
    """Home directory of profile *name*. Raises on an unsafe name."""
    if not is_safe_profile_name(name):
        raise InvalidProfileNameError(name)
    return profiles_root() / name


def profile_exists(name: str) -> bool:
    try:
        return profile_dir(name).is_dir()
    except InvalidProfileNameError:
        return False


def active_profile_file() -> Path:
    """The sticky selection file (one line: the profile name)."""
    return base_home() / _STICKY_FILE_NAME


def read_sticky_profile() -> Optional[str]:
    """Return the sticky profile name, or None. Fail-open."""
    try:
        f = active_profile_file()
        if f.is_file():
            return f.read_text(encoding="utf-8").strip() or None
    except Exception:
        pass
    return None


def _find_project_pin() -> Optional[str]:
    """Nearest ``./.polyrob/profile`` walking cwd up to the git root.

    Mirrors the walk in ``agents/task/agent/core/project_context.py``: a user in
    a subdirectory of a pinned project expects the pin to apply. Without a git
    root the walk checks the cwd only (never wanders to ``/``).
    """
    try:
        cwd = Path.cwd().resolve()
    except Exception:
        return None
    git_root = None
    p = cwd
    while True:
        if (p / ".git").exists():
            git_root = p
            break
        if p.parent == p:
            break
        p = p.parent
    stop = git_root if git_root is not None else cwd
    p = cwd
    while True:
        try:
            pin = p / _PIN_REL_PATH
            if pin.is_file():
                return pin.read_text(encoding="utf-8").strip() or None
        except Exception:
            return None
        if p == stop or p.parent == p:
            break
        p = p.parent
    return None


def _weak_tier_selection(name: str, source: str) -> Optional[ProfileSelection]:
    """Validate a pin/sticky name FAIL-OPEN: a broken ambient signal must not
    brick every CLI run — warn and fall through to the next tier instead."""
    if not is_safe_profile_name(name):
        sys.stderr.write(
            f"polyrob: ignoring {source} profile {name!r} (invalid name)\n")
        return None
    home = profiles_root() / name
    if not home.is_dir():
        sys.stderr.write(
            f"polyrob: ignoring {source} profile {name!r} (no such profile under "
            f"{profiles_root()}; create it with: polyrob profile create {name})\n")
        return None
    return ProfileSelection(name=name, home=home, source=source)


def resolve_active_profile(flag: Optional[str] = None) -> Optional[ProfileSelection]:
    """Resolve the active profile (tiers 1-5). Returns None for legacy mode.

    Strong tiers (flag/env) RAISE on an invalid or missing profile — an explicit
    selection must never silently degrade. Weak tiers (pin/sticky) warn + fall
    through instead.
    """
    if flag is not None and flag.strip():
        name = flag.strip()
        if not is_safe_profile_name(name):
            raise InvalidProfileNameError(name)
        home = profiles_root() / name
        if not home.is_dir():
            raise ProfileNotFoundError(name, home)
        return ProfileSelection(name=name, home=home, source="flag")

    if "POLYROB_PROFILE" in os.environ:
        name = os.environ["POLYROB_PROFILE"].strip()
        if not name:
            return None  # explicit "no profile" — disables pin/sticky too
        if not is_safe_profile_name(name):
            raise InvalidProfileNameError(name)
        home = profiles_root() / name
        if not home.is_dir():
            raise ProfileNotFoundError(name, home)
        # After activation the env carries the selection for children; keep
        # reporting the ORIGINAL tier (flag/pin/sticky) that chose it.
        source = (os.environ.get("POLYROB_PROFILE_SOURCE") or "").strip() or "env"
        return ProfileSelection(name=name, home=home, source=source)

    pin = _find_project_pin()
    if pin:
        sel = _weak_tier_selection(pin, "project_pin")
        if sel is not None:
            return sel

    sticky = read_sticky_profile()
    if sticky:
        sel = _weak_tier_selection(sticky, "sticky")
        if sel is not None:
            return sel

    return None


def _warn_mismatch_once(sel: ProfileSelection, var: str, actual: str) -> None:
    global _warned_mismatch
    if _warned_mismatch:
        return
    _warned_mismatch = True
    sys.stderr.write(
        f"polyrob: {sel.source} selects profile '{sel.name}' "
        f"({sel.home}) but {var} is already set to {actual} — the explicit "
        f"environment wins. Unset {var} or pass -P {sel.name} to force the "
        f"profile.\n")


def apply_profile_env(sel: ProfileSelection) -> None:
    """Point the two path seams at the selected profile (see module docstring).

    A strong tier OVERWRITES the homes; a weak tier DEFERS to an already-set
    environment variable (with a one-shot mismatch warning). Never clobbers an
    explicit ``POLYROB_PROJECT_DIR``.
    """
    # Pin the registry base BEFORE POLYROB_HOME moves inside the profile, so
    # in-profile `polyrob profile …` commands still see the shared registry.
    os.environ.setdefault("POLYROB_PROFILES_ROOT", str(profiles_root()))

    home = sel.home
    data = home / "data"
    if sel.source in ("flag", "env"):
        os.environ["POLYROB_HOME"] = str(home)
        os.environ["POLYROB_DATA_DIR"] = str(data)
    else:
        cur_home = os.environ.get("POLYROB_HOME")
        if cur_home and Path(cur_home).resolve() != home.resolve():
            _warn_mismatch_once(sel, "POLYROB_HOME", cur_home)
        os.environ.setdefault("POLYROB_HOME", str(home))
        cur_data = os.environ.get("POLYROB_DATA_DIR")
        if cur_data and Path(cur_data).resolve() != data.resolve():
            _warn_mismatch_once(sel, "POLYROB_DATA_DIR", cur_data)
        os.environ.setdefault("POLYROB_DATA_DIR", str(data))

    # Keep the workspace in the folder the user launched from (profile mode
    # sets POLYROB_DATA_DIR, which would otherwise move the workspace under the
    # data home). An explicit POLYROB_PROJECT_DIR always wins.
    os.environ.setdefault("POLYROB_PROJECT_DIR", str(Path.cwd()))

    # Stash the selection for child processes + the banner — but only when this
    # process actually runs on the profile's home (a deferred mismatch must not
    # make CHILDREN diverge from the parent).
    if os.environ.get("POLYROB_HOME") == str(home):
        os.environ["POLYROB_PROFILE"] = sel.name
        os.environ.setdefault("POLYROB_PROFILE_SOURCE", sel.source)


def activate_profile(flag: Optional[str] = None) -> Optional[ProfileSelection]:
    """Resolve + apply the profile env. THE one CLI-entry call (before load_env)."""
    os.environ[_RESOLVED_MARKER] = "1"
    sel = resolve_active_profile(flag)
    if sel is not None:
        apply_profile_env(sel)
    return sel


def warn_profile_fallback_once() -> None:
    """Wrong-home fallback guard (known failure mode): the sticky
    file names a profile but this process resolved nothing and ``POLYROB_HOME``
    is unset — any data it writes lands in the DEFAULT home, not the profile.
    Loud one-shot stderr warning; never raises, never blocks (spawners are
    expected to propagate POLYROB_HOME explicitly)."""
    global _warned_fallback
    if _warned_fallback:
        return
    _warned_fallback = True
    try:
        if os.environ.get("POLYROB_HOME") or os.environ.get(_RESOLVED_MARKER):
            return
        name = read_sticky_profile()
        if not name:
            return
        sys.stderr.write(
            f"polyrob: WARNING — POLYROB_HOME is unset but the active profile "
            f"is '{name}'. This process falls back to {polyrob_home()}, the "
            f"DEFAULT home — NOT profile '{name}'. Anything it writes lands in "
            f"the wrong profile. Launch via the polyrob CLI or export "
            f"POLYROB_HOME/POLYROB_DATA_DIR explicitly.\n")
    except Exception:
        pass


def foreign_profile_of_path(path) -> Optional[str]:
    """Name of the OTHER profile *path* points into, or ``None``.

    Cross-profile write guard (W4, follows the reference implementation's
    file-safety shape): a session running as one profile (or none) has no business touching
    another profile's home. Honestly: DEFENSE-IN-DEPTH, not a security boundary
    — same user, same filesystem; the bypass is
    ``POLYROB_ALLOW_CROSS_PROFILE=1`` or simply running with ``-P <name>``.
    """
    try:
        root = profiles_root().resolve()
        rel = Path(path).resolve().relative_to(root)
    except Exception:
        return None
    parts = rel.parts
    if not parts:
        return None
    name = parts[0]
    active = (os.environ.get("POLYROB_PROFILE") or "").strip()
    if name == active:
        return None
    return name


def cross_profile_access_allowed() -> bool:
    """The explicit opt-in for deliberate cross-profile file access."""
    from core.env import bool_env
    return bool_env("POLYROB_ALLOW_CROSS_PROFILE", False)


def _read_profile_yaml(home: Path) -> dict:
    """Read ``profile.yaml`` fail-open (missing/broken -> {})."""
    try:
        f = home / "profile.yaml"
        if not f.is_file():
            return {}
        import yaml
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def list_profiles() -> List[ProfileInfo]:
    """All named profiles on disk (safe names only), sorted by name."""
    root = profiles_root()
    out: List[ProfileInfo] = []
    try:
        if not root.is_dir():
            return out
        for p in sorted(root.iterdir()):
            if not p.is_dir() or not is_safe_profile_name(p.name):
                continue
            meta = _read_profile_yaml(p)
            out.append(ProfileInfo(
                name=p.name,
                home=p,
                display_name=str(meta.get("display_name") or ""),
                description=str(meta.get("description") or ""),
            ))
    except Exception:
        pass
    return out
