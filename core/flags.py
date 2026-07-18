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
from dataclasses import dataclass
from typing import Callable, Optional

from core.env import parse_bool
from core.flags_catalog import CATALOG

# Suffix-based so LLM_MAX_OUTPUT_TOKENS / POLYROB_PROJECT_SECRET_REFUSE stay visible.
# _SEED covers wallet seed material (PAYMENT_MASTER_SEED/MASTER_SEED — "KEEP
# SECRET!" per core/config.py); _HASH covers POLYROB_OWNER_PASSWORD_HASH (echoing
# the argon2 hash enables offline cracking of the console password).
_SECRET_SUFFIXES = (
    "_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_MNEMONIC", "_JWT", "_PRIVATE_KEY",
    "_SEED", "_HASH",
)

_TRUEISH_DOC = ("on", "true", "yes", "1")
_FALSEISH_DOC = ("off", "false", "no", "0")


def is_secret_flag(name: str) -> bool:
    """Whether a flag's value must be masked in any report output."""
    return name.upper().endswith(_SECRET_SUFFIXES)


def _infer_kind(default_doc: str) -> str:
    """Best-effort kind from the documented default's FIRST token ('bool' |
    'int' | 'str') — trailing prose like "`1440` (**720 POLYROB_LOCAL**)" must
    not demote a numeric default to an opaque string."""
    doc = default_doc.strip().strip("*")
    # e.g. "ON", "OFF", "ON (`\"1\"`)", "**ON**", "`1440` (**720 POLYROB_LOCAL**)"
    head = doc.split(" ")[0].strip("`'\"*").lower()
    if head in _TRUEISH_DOC or head in _FALSEISH_DOC:
        return "bool"
    if head.lstrip("-").isdigit():
        return "int"
    return "str"


def _default_value(kind: str, default_doc: str):
    """Materialize the documented default into a typed value where possible."""
    doc = default_doc.strip().strip("*")
    head = doc.split(" ")[0].strip("`'\"").lower()
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
    kind: str  # 'bool' | 'int' | 'str'


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

for _name, _group, _default in CATALOG:
    _flag = Flag(name=_name, group=_group, default_doc=_default, kind=_infer_kind(_default))
    if "<" in _name:
        PATTERNS.append(_flag)
    else:
        REGISTRY.setdefault(_name, _flag)


DynamicDefault = Callable[[str], Optional[tuple]]


def resolve_flag(name: str, env: dict, dynamic_default: Optional[DynamicDefault] = None) -> ResolvedFlag:
    """Resolve one flag against an env mapping (pure; no os.environ read).

    Precedence: explicit non-blank env value > ``dynamic_default(name)`` (a
    ``(value, source_label)`` tuple or None) > the documented static default.
    Secret flags report ``(set, masked)`` / ``(unset)`` instead of the value.
    """
    flag = REGISTRY[name]
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
                return ResolvedFlag(name, flag.group, str(raw), "env")
        return ResolvedFlag(name, flag.group, str(raw), "env")

    if dynamic_default is not None:
        dyn = dynamic_default(name)
        if dyn is not None:
            value, source = dyn
            return ResolvedFlag(name, flag.group, value, source)

    if is_secret_flag(name):
        return ResolvedFlag(name, flag.group, "(unset)", "default")
    return ResolvedFlag(name, flag.group, _default_value(flag.kind, flag.default_doc), "default")


def resolve_all(env: dict, dynamic_default: Optional[DynamicDefault] = None) -> list[ResolvedFlag]:
    """Resolve every registered flag, in catalog (= documentation) order."""
    return [resolve_flag(name, env, dynamic_default) for name in REGISTRY]
