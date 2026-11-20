"""The console's read of one setting's effective value (113 W6 item 1).

``core.prefs.display_effective`` takes an unset list setting's built-in default
from the flags catalog, whose display word for an empty list is ``"empty"``. A
list editor that receives that word shows it as an entry and would store it on
Save. The console sends an empty list instead. ``core/prefs.py`` is at its
size ceiling, so the rule lives here, beside the one reader that needs it.
"""
from pathlib import Path
from typing import Optional

from core.prefs import PREF_SCHEMA, display_effective as _core_display_effective

#: The catalog's display words for "no entries" (``docs/CONFIGURATION.md``).
_EMPTY_WORDS = frozenset({"", "empty", "none", "(empty)"})


def display_effective(key: str, user_id: Optional[str], home_dir: Path | str,
                      instance_id: Optional[str] = None) -> tuple[object, str]:
    """``core.prefs.display_effective``, with an empty list sent as ``[]``."""
    value, source = _core_display_effective(key, user_id, home_dir, instance_id)
    spec = PREF_SCHEMA.get(key)
    if (spec is not None and spec.type == "list" and isinstance(value, str)
            and value.strip().lower() in _EMPTY_WORDS):
        return [], source
    return value, source


def control_value(key: str, value: object, source: str) -> object:
    """The value a Settings CONTROL starts with — not the display words.

    An unset setting's built-in default is display text (``"not set (any
    pair)"``, ``"server clock"``, ```auto```). A control that takes it as its
    value shows prose in a text box, cannot hold it in a number box or a
    select, and so counts as "changed" — and Save then writes a setting the
    owner never touched. A value the owner or the operator set is the value; a
    default is the control's value only when it is a valid value of the
    setting, and a text setting's default is never one (it is a description).
    """
    src = str(source or "")
    if src.startswith("pref") or src.startswith("env"):
        return value
    spec = PREF_SCHEMA.get(key)
    if spec is None or value is None or spec.type == "str":
        return None
    from core.prefs import validate_pref
    ok, parsed, _err = validate_pref(key, value)
    return parsed if ok else None


def source_words(source: str) -> str:
    """``default(default (no bundle))`` → ``default (no bundle)``: the catalog's
    resolver label, without its wrapper."""
    src = str(source or "")
    if src.startswith("default(") and src.endswith(")"):
        return src[len("default("):-1]
    return src
