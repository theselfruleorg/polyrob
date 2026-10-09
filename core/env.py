"""Canonical env-flag parser for the whole repo. One falsey-set, one parser."""
import math
import os
from typing import Mapping, Optional

_FALSEY = ("none", "off", "false", "0", "no", "")
_EXPLICIT_TRUE = frozenset(("true", "yes", "on", "1"))


def parse_bool(value, default: bool, *, strict: bool = False) -> bool:
    """Value-based falsey-set parser (for already-fetched values, e.g. pydantic validators).

    strict=True allows only explicit true/yes/on/1; other supplied values are false.
    None -> default; otherwise True unless the value is in _FALSEY ('' counts as falsey).
    Note this differs from ``bool_env``'s blank-env semantics: here an explicit ``""``
    value is treated as falsey (False), whereas ``bool_env`` treats a blank/unset env
    var as ``default``. Both are intentional — see module docstring.
    """
    if value is None:
        return default
    if strict:
        return str(value).strip().lower() in _EXPLICIT_TRUE
    return str(value).strip().lower() not in _FALSEY


def bool_env(name: str, default: bool, *, strict: bool = False) -> bool:
    """Read a boolean env var with POLYROB's canonical falsey-set semantics.

    Returns ``default`` when unset/blank; otherwise True unless the value is in
    {none, off, false, 0, no, ""}. Use everywhere instead of open-coding
    ``os.getenv(...).lower() == 'true'`` so default-ON and default-OFF flags share
    one parser (the reflection-gate bug was a parser/source mismatch).
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return parse_bool(raw, default, strict=strict)


def float_env(name: str, default: float) -> float:
    """Float twin of :func:`int_env` (018 P5a — there was NO float SSOT; every
    float flag was a raw crash-prone ``float(os.getenv(...))``). Unset, blank,
    unparsable (incl. the ``"none"``/``"off"`` disable idioms), or non-finite
    => *default*. Non-finite matters: ``"inf"``/``"nan"`` parse as valid
    floats, and a NaN/inf threshold turns a gate (e.g. RUN_BUDGET_USD) into a
    silent never-trips while the operator believes it is active (2026-07-23
    validation)."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if not math.isfinite(val):
        return default
    return val


def int_env(name: str, default: int) -> int:
    """Read an integer env var, returning ``default`` on missing or non-integer value."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def parse_opt_float(value) -> Optional[float]:
    """Value-based optional-float parse: None/blank/unparsable/non-finite -> None.

    The finite check matters for money caps: ``"inf"`` parses as a valid float
    and turns a ceiling (e.g. WALLET_DAILY_CAP_USD) into a silent never-trips.
    """
    if value is None or not str(value).strip():
        return None
    try:
        val = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def bool_from(env: Optional[Mapping], name: str, default: bool) -> bool:
    """:func:`bool_env` over an injected Mapping (``None`` -> ``os.environ``).

    For the wallet/pairing/access style of testable, env-injected config —
    those modules used to carry private opt-in truth-set forks that disagreed
    with the repo falsey-set on values like ``"enabled"``.
    """
    src = os.environ if env is None else env
    raw = src.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return parse_bool(raw, default)


def float_from(env: Optional[Mapping], name: str, default: float) -> float:
    """:func:`float_env` over an injected Mapping (``None`` -> ``os.environ``)."""
    src = os.environ if env is None else env
    val = parse_opt_float(src.get(name))
    return default if val is None else val


_PRODUCTION_NAMES = frozenset(("production", "prod"))


def is_production_env(resolved: Optional[str] = None) -> bool:
    """True when ANY deployment-environment name says production.

    The repo has four names for one fact: ``ENVIRONMENT`` (what prod's
    ``/etc/polyrob/polyrob.env`` sets), ``POLYROB_ENV``, ``CONFIG_ENV``/``ENV``
    (``core.bootstrap.load_env``'s ``resolved``), and the ``PRODUCTION`` bool.
    A boot refusal keyed on only one of them silently passes on the others.
    """
    names = [resolved, os.getenv("ENVIRONMENT"), os.getenv("POLYROB_ENV"),
             os.getenv("CONFIG_ENV"), os.getenv("ENV")]
    if any(str(n or "").strip().lower() in _PRODUCTION_NAMES for n in names):
        return True
    return bool_env("PRODUCTION", False)
