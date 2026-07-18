"""Canonical env-flag parser for the whole repo. One falsey-set, one parser."""
import os

_FALSEY = ("none", "off", "false", "0", "no", "")


def parse_bool(value, default: bool) -> bool:
    """Value-based falsey-set parser (for already-fetched values, e.g. pydantic validators).

    None -> default; otherwise True unless the value is in _FALSEY ('' counts as falsey).
    Note this differs from ``bool_env``'s blank-env semantics: here an explicit ``""``
    value is treated as falsey (False), whereas ``bool_env`` treats a blank/unset env
    var as ``default``. Both are intentional — see module docstring.
    """
    if value is None:
        return default
    return str(value).strip().lower() not in _FALSEY


def bool_env(name: str, default: bool) -> bool:
    """Read a boolean env var with POLYROB's canonical falsey-set semantics.

    Returns ``default`` when unset/blank; otherwise True unless the value is in
    {none, off, false, 0, no, ""}. Use everywhere instead of open-coding
    ``os.getenv(...).lower() == 'true'`` so default-ON and default-OFF flags share
    one parser (the reflection-gate bug was a parser/source mismatch).
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return parse_bool(raw, default)


def float_env(name: str, default: float) -> float:
    """Float twin of :func:`int_env` (018 P5a — there was NO float SSOT; every
    float flag was a raw crash-prone ``float(os.getenv(...))``). Unset, blank,
    or unparsable (incl. the ``"none"``/``"off"`` disable idioms) => *default*."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def int_env(name: str, default: int) -> int:
    """Read an integer env var, returning ``default`` on missing or non-integer value."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
