"""Runtime-enumerable env-flag registry (Wave D / SA-05).

POLYROB has ~300 env flags; until this module the only enumeration was the
hand-maintained table in ``docs/CONFIGURATION.md`` and the only resolution was
scattered ``bool_env``/``os.getenv`` call sites — so "shipped dark" flags were
structurally invisible. This registry makes the flag surface a runtime object:

- ``REGISTRY``            name -> :class:`Flag` (group + documented default),
                          seeded from :mod:`core.flags_catalog` (extracted from
                          docs/CONFIGURATION.md via ``scripts/gen_flags_catalog.py``;
                          the contract test asserts doc rows ⊆ registry so the
                          two cannot drift apart).
- ``resolve_flag``/``resolve_all``  name -> resolved value + source, using the
                          canonical parsers in :mod:`core.env`. Callers may pass
                          a ``dynamic_default`` hook so posture/local-derived
                          defaults resolve live — see
                          ``core/config_policy/flag_defaults.py`` (the hook stays
                          caller-injected, so plain resolution keeps its static
                          defaults).

Secrets (keys/tokens/passwords) are never echoed: :func:`is_secret_flag` masks
their values in every resolution, so ``doctor --flags`` output is safe to paste.
No behavior change to any flag consumer — same parsers underneath.
"""
from dataclasses import dataclass, replace
from typing import Callable, Optional

from core.env import parse_bool
from core.flags_catalog import ACCESS, CATALOG
from core.security.redaction import redact_config_urls

_TRUEISH_DOC = ("on", "true", "yes", "1")
_FALSEISH_DOC = ("off", "false", "no", "0")


def is_secret_flag(name: str) -> bool:
    """Whether a flag's value must be masked in any report output.

    A cataloged flag (an exact row or a ``<...>`` pattern row) is secret when
    its row's Access column says ``secret`` (``core.flags_catalog.ACCESS``,
    generated from docs/CONFIGURATION.md) — the class is declared per row, never
    derived from the spelling of a name. A name the catalog does not know (an
    arbitrary env line a report shows) is masked when it LOOKS like a credential
    (``core.secrets.is_secret_key``, or an RPC URL that carries its key), so an
    unknown value fails safe toward masking.
    """
    cls = access_class(name)
    if cls is not None:
        return cls == "secret"
    try:
        from core.secrets import is_secret_key
        return bool(is_secret_key(name)) or "_RPC" in name.upper()
    except Exception:             # pragma: no cover — fail toward masking
        return True


def access_class(name: str) -> Optional[str]:
    """``console`` | ``file`` | ``secret`` for a cataloged flag, else None."""
    cls = ACCESS.get(name)
    if cls is not None:
        return cls
    pattern = pattern_flag_for(name)
    return ACCESS.get(pattern.name) if pattern is not None else None


def _enum_kind(name: str) -> Optional[str]:
    """``'enum'`` when *name* has a valid-value table in the enum SSOT
    (``core.config_policy.flag_enums.FLAG_ENUMS``), else None.

    030 WS-F2 / E9: an enum flag whose values happen to be numeric strings
    (AGENT_COMPUTE_POSTURE — `0`, a 0-3 ladder) must never depend on the
    textual default heuristic below to avoid being kinded bool/int — the SSOT
    is consulted FIRST, deterministically. Lazy import + fail-open so a future
    import cycle can only degrade to the heuristic, never break the registry.
    """
    try:
        from core.config_policy.flag_enums import flag_enum_values
    except Exception:
        return None
    return "enum" if flag_enum_values(name) is not None else None


def _infer_kind(default_doc: str) -> str:
    """Best-effort kind from the documented default's FIRST token ('bool' |
    'int' | 'str') — trailing prose like "`1440` (**720 POLYROB_LOCAL**)" must
    not demote a numeric default to an opaque string.

    A backtick-quoted bare digit (`` `0` ``, `` `1` ``) is the catalog's NUMERIC
    grammar, never a bool — AGENT_COMPUTE_POSTURE (`0`, a 0-3 ladder) used to
    resolve as kind=bool, so `doctor --flags` printed `True` for posture 2
    (026 P0.2). Bool defaults are documented as ON/OFF words. Enum-shaped
    flags never reach this heuristic (``_enum_kind`` wins first).
    """
    doc = default_doc.strip().strip("*")
    # e.g. "ON", "OFF", "ON (`\"1\"`)", "**ON**", "`1440` (**720 POLYROB_LOCAL**)"
    raw_head = doc.split(" ")[0]
    head = raw_head.strip("`'\"*").lower()
    quoted_numeric = raw_head.startswith("`") and head.lstrip("-").isdigit()
    if not quoted_numeric and (head in _TRUEISH_DOC or head in _FALSEISH_DOC):
        return "bool"
    if head.lstrip("-").isdigit():
        return "int"
    return "str"


def _default_value(kind: str, default_doc: str):
    """Materialize the documented default into a typed value where possible."""
    doc = default_doc.strip().strip("*")
    head = doc.split(" ")[0].strip("`'\"").lower()
    if kind == "enum":
        # The bare first token, case preserved ("supervised", "0") — never the
        # raw backticked doc cell, and never coerced to bool/int.
        raw = doc.split(" ")[0].strip("`'\"*")
        return None if raw.lower() in ("unset", "", "—", "-") else raw
    if kind == "bool":
        return head in _TRUEISH_DOC
    if kind == "int":
        try:
            return int(doc.split(" ")[0].strip("`'\"*"))
        except ValueError:
            return doc
    if head in ("unset", "", "—", "-"):
        return None
    return doc


@dataclass(frozen=True)
class Flag:
    name: str
    group: str
    default_doc: str
    kind: str  # 'bool' | 'int' | 'str' | 'enum' (valid values in flag_enums SSOT)
    description: str = ""  # 030 WS-F1: the doc's "What it does" cell (compressed)
    tier: str = "advanced"  # 067 F1: 'public' | 'advanced' | 'internal' (generator-derived)


@dataclass(frozen=True)
class ResolvedFlag:
    name: str
    group: str
    value: object
    source: str  # 'env' | 'default' | dynamic label e.g. 'default(posture:full)'


REGISTRY: dict[str, Flag] = {}
# Dynamic name patterns (e.g. POLYROB_<PROVIDER>_MODEL) — documented but not
# enumerable; kept for reference/reporting, not resolved.
PATTERNS: list[Flag] = []

for _name, _group, _default, *_rest in CATALOG:
    _flag = Flag(name=_name, group=_group, default_doc=_default,
                 kind=_enum_kind(_name) or _infer_kind(_default),
                 description=(_rest[0] if _rest else ""),
                 tier=(_rest[1] if len(_rest) > 1 else "advanced"))
    if "<" in _name:
        PATTERNS.append(_flag)
    else:
        REGISTRY.setdefault(_name, _flag)


def pattern_flag_for(name: str) -> Optional[Flag]:
    """Match *name* against the dynamic ``<...>`` catalog patterns (e.g.
    ``POLYROB_<PROVIDER>_MODEL`` ← ``POLYROB_OPENROUTER_MODEL``). Placeholders
    match ``[A-Z0-9_]+`` (mirrors ``core.prefs.catalog_lookup``); returns the
    pattern Flag or None."""
    import re
    for f in PATTERNS:
        rx = re.sub(r"<[A-Z_]+>", "[A-Z0-9_]+", re.escape(f.name))
        if re.fullmatch(rx, name):
            return f
    return None


def flag_for(name: str) -> Optional[Flag]:
    """The catalog Flag for a concrete *name*: an exact row, else the ``<...>``
    pattern row it matches (renamed to *name*), else None. A family documented
    only by its pattern row (``OWNER_<SURFACE>_ID``) resolves like any flag."""
    flag = REGISTRY.get(name)
    if flag is not None:
        return flag
    pattern = pattern_flag_for(name)
    return replace(pattern, name=name) if pattern is not None else None


DynamicDefault = Callable[[str], Optional[tuple]]


def resolve_flag(name: str, env: dict, dynamic_default: Optional[DynamicDefault] = None) -> ResolvedFlag:
    """Resolve one flag against an env mapping (pure; no os.environ read).

    Precedence: explicit non-blank env value > ``dynamic_default(name)`` (a
    ``(value, source_label)`` tuple or None) > the documented static default.
    Secret flags report ``(set, masked)`` / ``(unset)`` instead of the value.
    """
    flag = flag_for(name)
    if flag is None:
        raise KeyError(name)
    raw = env.get(name)
    if raw is not None and str(raw).strip() != "":
        if is_secret_flag(name):
            return ResolvedFlag(name, flag.group, "(set, masked)", "env")
        if flag.kind == "bool":
            return ResolvedFlag(name, flag.group, parse_bool(raw, False), "env")
        if flag.kind == "int":
            try:
                return ResolvedFlag(name, flag.group, int(str(raw).strip()), "env")
            except ValueError:
                return ResolvedFlag(name, flag.group, redact_config_urls(str(raw)), "env")
        return ResolvedFlag(name, flag.group, redact_config_urls(str(raw)), "env")

    if dynamic_default is not None:
        dyn = dynamic_default(name)
        if dyn is not None:
            value, source = dyn
            value = "(set, masked)" if is_secret_flag(name) and value else redact_config_urls(value)
            return ResolvedFlag(name, flag.group, value, source)

    if is_secret_flag(name):
        return ResolvedFlag(name, flag.group, "(unset)", "default")
    return ResolvedFlag(name, flag.group, _default_value(flag.kind, flag.default_doc), "default")


def resolve_all(env: dict, dynamic_default: Optional[DynamicDefault] = None) -> list[ResolvedFlag]:
    """Resolve every registered flag, in catalog (= documentation) order."""
    return [resolve_flag(name, env, dynamic_default) for name in REGISTRY]
