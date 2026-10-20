"""Posture members — the ONE resolver for a bundle member flag (owner decision D7,
2026-10-10: "A + one override").

A posture bundle (``POLYROB_LOCAL``, ``AUTONOMY_ENABLED``, ``AUTONOMY_MODE``,
``AUTONOMY_POSTURE``, ``AGENT_BUILDER_MODE``, ``AGENT_COMPUTE_POSTURE``) is the
ONLY source of its member flags. A member is NOT an env setting of its own:

    value = the bundle default  <-  the POLYROB_OVERRIDES entry for it

``POLYROB_OVERRIDES=GIT_TOOLS_ENABLED=false,KB_ENABLED=true`` is the one line
that changes a single member. It is Access ``file`` (only the operator env file
or ``polyrob config set`` writes it — never an agent tool, a console write or a
chat preference).

A direct env value of a member name (``GIT_TOOLS_ENABLED=false``) is IGNORED.
Nothing refuses: the process starts, and ``polyrob doctor`` plus the status
``posture`` section name the line with its exact replacement
(:func:`member_env_lines`, reported through ``retired_flags``).

Every reader of a member calls :func:`member_flag`. The reverse flags contract
(``tests/unit/core/test_flags_reverse.py``) fails on any ``bool_env``/``getenv``
read of a member name, because members have no catalog row.

Import order: after ``builder_mode``/``autonomy_posture``/``compute_posture``,
before ``capability_toggles``/``autonomy_config`` (their readers import this).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, FrozenSet, List, Mapping, Optional, Tuple

from core.env import _EXPLICIT_TRUE, _FALSEY
from core.config_policy.local_profile import (
    _AUTONOMY_LOCAL_FLAGS,
    _SAFE_LOCAL_FLAGS,
    local_mode_enabled,
)
from core.config_policy.autonomy_mode import _MODE_CAPABILITY_FLAGS, full_autonomy_enabled
from core.config_policy.autonomy_posture import (
    _POSTURE_FULL_FLAGS,
    _POSTURE_OWNER_VISIBLE_FLAGS,
    autonomy_enabled,
    autonomy_posture,
)
from core.config_policy.builder_mode import _BUILD_FLAGS, _SHIP_FLAGS, effective_builder_mode
from core.config_policy.compute_posture import compute_posture

logger = logging.getLogger(__name__)

#: The env line that changes one member (Access ``file``).
OVERRIDES_ENV = "POLYROB_OVERRIDES"

#: ``AGENT_COMPUTE_POSTURE`` members: name -> the posture at which it turns ON.
COMPUTE_POSTURE_MEMBERS: Dict[str, int] = {
    "CODE_EXEC_DOCKER_PERSISTENT": 1,
    "SHELL_TOOLS_ENABLED": 1,
    "SELF_ENV_ENABLED": 2,
}

#: Members whose value outside every bundle is ON (the bundle only names them).
_BASE_ON: FrozenSet[str] = frozenset({"GOAL_COMPLETION_JUDGE"})

#: Every member flag: the union of the bundle sets. The doc's "Posture bundles"
#: table is pinned to this by ``tests/unit/core/test_posture_bundles_doc.py``.
MEMBER_FLAGS: FrozenSet[str] = frozenset(
    set(_SAFE_LOCAL_FLAGS) | set(_AUTONOMY_LOCAL_FLAGS) | set(_MODE_CAPABILITY_FLAGS)
    | set(_POSTURE_FULL_FLAGS) | set(_POSTURE_OWNER_VISIBLE_FLAGS)
    | set(_BUILD_FLAGS) | set(_SHIP_FLAGS) | set(COMPUTE_POSTURE_MEMBERS)
)


def is_member(name: str) -> bool:
    return str(name or "").strip().upper() in MEMBER_FLAGS


# --- POLYROB_OVERRIDES parsing ---------------------------------------------------

@dataclass(frozen=True)
class OverrideProblem:
    """One ``POLYROB_OVERRIDES`` entry that is ignored, and why."""
    entry: str
    name: str
    kind: str          # malformed | unknown | not_member | bad_value
    text: str


def _known_flag(name: str) -> bool:
    try:
        from core.flags import REGISTRY, pattern_flag_for
        return name in REGISTRY or pattern_flag_for(name) is not None
    except Exception:  # noqa: BLE001 — a missing catalog never hides a member
        return False


@lru_cache(maxsize=32)
def parse_overrides(raw: Optional[str]) -> Tuple[Tuple[Tuple[str, bool], ...],
                                                 Tuple[OverrideProblem, ...]]:
    """``(((name, value), ...), (problem, ...))`` for one ``POLYROB_OVERRIDES``
    value. Pure. A later entry for the same name wins. An entry that is not
    ``MEMBER=bool`` is a problem and is ignored (the bundle default stays)."""
    values: Dict[str, bool] = {}
    problems: List[OverrideProblem] = []
    for part in str(raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        name, sep, value = entry.partition("=")
        name, value = name.strip().upper(), value.strip()
        if not sep or not name:
            problems.append(OverrideProblem(entry, name, "malformed",
                                            f"{entry!r} is not NAME=value"))
            continue
        if name not in MEMBER_FLAGS:
            if _known_flag(name):
                problems.append(OverrideProblem(
                    entry, name, "not_member",
                    f"{name} is not a posture member — set {name} on its own line"))
            else:
                problems.append(OverrideProblem(entry, name, "unknown",
                                                f"{name} is not a known flag"))
            continue
        low = value.lower()
        if low in _EXPLICIT_TRUE:
            values[name] = True
        elif low and low in _FALSEY:
            values[name] = False
        else:
            problems.append(OverrideProblem(
                entry, name, "bad_value",
                f"{name}={value!r} is not true/false — the bundle default applies"))
    return tuple(values.items()), tuple(problems)


def _raw_overrides(env: Optional[Mapping[str, str]] = None) -> str:
    src = os.environ if env is None else env
    return str(src.get(OVERRIDES_ENV) or "")


def overrides(env: Optional[Mapping[str, str]] = None) -> Dict[str, bool]:
    """name -> value for every valid ``POLYROB_OVERRIDES`` entry."""
    return dict(parse_overrides(_raw_overrides(env))[0])


def override_problems(env: Optional[Mapping[str, str]] = None) -> Tuple[OverrideProblem, ...]:
    return parse_overrides(_raw_overrides(env))[1]


# --- the resolver ------------------------------------------------------------

def _bundle_sources(name: str) -> List[str]:
    """The bundles that turn *name* ON right now (empty = none does)."""
    out: List[str] = []
    if name in _SAFE_LOCAL_FLAGS and local_mode_enabled():
        out.append("POLYROB_LOCAL")
    if name in _AUTONOMY_LOCAL_FLAGS and local_mode_enabled() and autonomy_enabled():
        out.append("POLYROB_LOCAL + AUTONOMY_ENABLED")
    if name in _MODE_CAPABILITY_FLAGS and full_autonomy_enabled():
        out.append("AUTONOMY_MODE=autonomous")
    posture = autonomy_posture()
    if (posture == "full" and name in _POSTURE_FULL_FLAGS) or (
            posture == "owner-visible" and name in _POSTURE_OWNER_VISIBLE_FLAGS):
        out.append(f"AUTONOMY_POSTURE={posture}")
    if name in _BUILD_FLAGS or name in _SHIP_FLAGS:
        mode = effective_builder_mode()
        if (mode in ("build", "ship") and name in _BUILD_FLAGS) or (
                mode == "ship" and name in _SHIP_FLAGS):
            out.append(f"AGENT_BUILDER_MODE={mode}")
    level = COMPUTE_POSTURE_MEMBERS.get(name)
    if level is not None and compute_posture() >= level:
        out.append(f"AGENT_COMPUTE_POSTURE>={level}")
    return out


def bundle_default(name: str) -> bool:
    """The member's value when no override names it. Fail-closed: a bundle
    probe that raises counts as OFF (a base-ON member stays ON)."""
    if name in _BASE_ON:
        return True
    try:
        if _bundle_sources(name):
            return True
    except Exception:  # noqa: BLE001
        logger.debug("bundle default probe failed for %s", name, exc_info=True)
        return False
    if name == "INVOICE_CARD_ENABLED":
        # 046 phase 2: an instance that invoices at all renders the card.
        return member_flag("X402_INVOICE_ENABLED")
    return False


def member_flag(name: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """THE value of a posture member: its ``POLYROB_OVERRIDES`` entry when one
    is valid, else the bundle default. *env* injects the mapping the override
    line is read from (``None`` = ``os.environ``); a direct env value of *name*
    is never read."""
    if name not in MEMBER_FLAGS:
        raise KeyError(f"{name} is not a posture member")
    ov = overrides(env)
    if name in ov:
        return ov[name]
    return bundle_default(name)


def member_source(name: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Where the member's value comes from, for a status/doctor line."""
    if name in overrides(env):
        return OVERRIDES_ENV
    try:
        srcs = _bundle_sources(name)
    except Exception:  # noqa: BLE001
        return "default (probe failed)"
    if srcs:
        return " + ".join(srcs)
    if name in _BASE_ON:
        return "built-in"
    if name == "INVOICE_CARD_ENABLED" and member_flag("X402_INVOICE_ENABLED"):
        return "X402_INVOICE_ENABLED"
    return "default (no bundle)"


def member_raw(name: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The override value as text (``"true"``/``"false"``), or None — for a
    reader that shows a raw operator value (the preferences display)."""
    ov = overrides(env)
    if name not in ov:
        return None
    return "true" if ov[name] else "false"


# --- writers and reporting -----------------------------------------------------

def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    low = str(value).strip().lower()
    if low in _EXPLICIT_TRUE:
        return "true"
    if low and low in _FALSEY:
        return "false"
    return str(value).strip()


def with_override(raw: Optional[str], name: str, value) -> str:
    """*raw* (a ``POLYROB_OVERRIDES`` value) with ``name=value`` set; every other
    entry is kept in order. ``value=None`` removes the entry."""
    name = str(name).strip().upper()
    kept = []
    for part in str(raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        if entry.partition("=")[0].strip().upper() == name:
            continue
        kept.append(entry)
    if value is not None:
        kept.append(f"{name}={_fmt(value)}")
    return ",".join(kept)


def set_line(name: str, value="true") -> str:
    """The env line an owner writes to set *name*: ``POLYROB_OVERRIDES=NAME=v``
    for a posture member, ``NAME=v`` for any other flag."""
    if name in MEMBER_FLAGS:
        return f"{OVERRIDES_ENV}={name}={_fmt(value)}"
    return f"{name}={_fmt(value)}"


def member_display_default(name: Optional[str]):
    """``(value, "default(<source>)")`` for a member, else None — the honest
    display default of a preference bound to a member. Fail-open (None)."""
    try:
        if name and name in MEMBER_FLAGS:
            return member_flag(name), f"default({member_source(name)})"
    except Exception:  # noqa: BLE001
        logger.debug("member display default failed for %s", name, exc_info=True)
    return None


def flag_raw(name: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The operator's raw value for any flag: a member's override entry (its
    direct env line is ignored), else the env value of *name*."""
    if name in MEMBER_FLAGS:
        return member_raw(name, env)
    src = os.environ if env is None else env
    return src.get(name)


def setdefault_flag(env, name: str, value) -> None:
    """``env.setdefault(name, value)`` for any flag: a member goes into the
    override line, and only when no override entry names it yet (an operator
    value still wins, as with ``setdefault``)."""
    if name not in MEMBER_FLAGS:
        env.setdefault(name, value)
        return
    if name in overrides(env):
        return
    env[OVERRIDES_ENV] = with_override(_raw_overrides(env), name, value)


def upsert_flag(path, name: str, value, *, secure: bool = True) -> str:
    """Write one flag to the env file at *path*: a member becomes (or updates)
    its entry in the file's ``POLYROB_OVERRIDES`` line, and a stale direct line
    of the member is removed. Returns the env key written."""
    from pathlib import Path

    from core.env_file import read_env_file, remove_env_var, upsert_env_var
    path = Path(path)
    if name not in MEMBER_FLAGS:
        upsert_env_var(path, name, str(value), secure=secure)
        return name
    current = read_env_file(path).get(OVERRIDES_ENV, "")
    upsert_env_var(path, OVERRIDES_ENV, with_override(current, name, value), secure=secure)
    remove_env_var(path, name, secure=secure)
    return OVERRIDES_ENV


def members_set_directly(env: Optional[Mapping[str, str]] = None) -> List[Tuple[str, str]]:
    """``[(name, raw value)]`` for every member an env still sets on its own
    line (ignored)."""
    src = os.environ if env is None else env
    out = []
    for name in sorted(MEMBER_FLAGS):
        raw = src.get(name)
        if raw is not None and str(raw).strip() != "":
            out.append((name, str(raw).strip()))
    return out


def replacement_line(env: Optional[Mapping[str, str]] = None) -> str:
    """The ONE ``POLYROB_OVERRIDES=…`` line that keeps every directly-set
    member's value (merged into the existing override line; an existing
    override entry for the same name wins, because it is what runs today)."""
    src = os.environ if env is None else env
    raw = _raw_overrides(src)
    current = overrides(src)
    for name, value in members_set_directly(src):
        if name not in current:
            raw = with_override(raw, name, value)
    return f"{OVERRIDES_ENV}={raw}"


def member_env_lines(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Doctor lines (WARN): each member still set directly, each ignored
    override entry. Empty when the env is clean."""
    src = os.environ if env is None else env
    out = []
    for name, value in members_set_directly(src):
        out.append(
            f"WARN posture member {name} is set directly and ignored: replace the "
            f"line with {OVERRIDES_ENV}={name}={_fmt(value)}")
    if len(members_set_directly(src)) > 1:
        out.append(f"WARN one line for all of them: {replacement_line(src)}")
    for p in override_problems(src):
        out.append(f"WARN {OVERRIDES_ENV} entry ignored: {p.text}")
    return out


def ignored_member_health(env: Optional[Mapping[str, str]] = None) -> Optional[Tuple[str, str, str]]:
    """The ``/status`` health item for member lines that are set directly and
    ignored (080 WS-1), as ``(key, text, remedy)`` — or None when the env is clean."""
    src = os.environ if env is None else env
    direct = members_set_directly(src)
    if not direct:
        return None
    n = len(direct)
    text = (f"{n} line{'s' if n != 1 else ''} in polyrob.env "
            f"{'are' if n != 1 else 'is'} ignored (posture members) — delete them "
            f"or move them into {OVERRIDES_ENV}")
    return ("member_env_lines_ignored", text, f"one line for all of them: {replacement_line(src)}")


_WARNED = False


def warn_member_env_once(env: Optional[Mapping[str, str]] = None) -> None:
    """One startup WARN in the log (never a refusal)."""
    global _WARNED
    if _WARNED:
        return
    lines = member_env_lines(env)
    if lines:
        _WARNED = True
        for line in lines:
            logger.warning("%s", line[len("WARN "):] if line.startswith("WARN ") else line)


__all__ = [
    "COMPUTE_POSTURE_MEMBERS", "MEMBER_FLAGS", "OVERRIDES_ENV", "OverrideProblem",
    "bundle_default", "ignored_member_health", "flag_raw", "is_member", "member_display_default", "member_env_lines", "member_flag", "member_raw",
    "member_source", "members_set_directly", "override_problems", "overrides",
    "parse_overrides", "replacement_line", "set_line", "setdefault_flag", "upsert_flag",
    "warn_member_env_once", "with_override",
]
