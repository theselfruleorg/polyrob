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
