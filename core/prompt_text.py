"""The system prompt's prose, read from ``data/prompts/system/<block>.md`` (081 WS-3.4).

Prose has no logic and logic has no prose: a block file holds the words, and
``agents/task/agent/prompts.py`` keeps which block renders, under which
condition and in what order (the foundation order stays in
``foundation_layers.py``). A text change is a data-file change, sized by
``tests/unit/core/test_prompt_text.py`` and seen whole in the prompt-surface
golden.

File format:

* one file per block; its text is the key ``<block>``;
* a line ``<!-- part: <name> -->`` starts a named part, key ``<block>.<name>``
  (text before the first part line, if any, is the ``<block>`` key);
* one trailing newline of each text is dropped, so a file ends normally;
* the text is a ``str.format`` template: ``{name}`` is a placeholder and a
  literal brace is written ``{{`` / ``}}`` — EVERY text is formatted, with or
  without placeholders, so one rule holds for every file.

The engine is ``core/copy/_engine.py``: under pytest a missing key or a missing
placeholder value raises; in a running process it degrades and logs once.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict

from core.copy._engine import build_copy_layer

logger = logging.getLogger(__name__)

SYSTEM_DIR = Path(__file__).resolve().parents[1] / "data" / "prompts" / "system"

_PART = re.compile(r"^<!-- part: ([a-z0-9_]+) -->\n", re.M)


def load_blocks(directory: Path = SYSTEM_DIR) -> Dict[str, str]:
    """``{key: text}`` for every ``*.md`` block in *directory* (see the module doc)."""
    out: Dict[str, str] = {}
    for path in sorted(directory.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        pieces = _PART.split(raw)
        head, rest = pieces[0], pieces[1:]
        if head.strip() or not rest:
            out[path.stem] = _trim(head)
        for name, body in zip(rest[::2], rest[1::2]):
            out[f"{path.stem}.{name}"] = _trim(body)
    return out


def _trim(text: str) -> str:
    return text[:-1] if text.endswith("\n") else text


STRINGS = load_blocks()
_t, has, missing_keys = build_copy_layer(
    STRINGS, label="prompt text", file_hint="data/prompts/system/", logger=logger)


def prompt_text(key: str, **kw) -> str:
    """The text of *key*, formatted with *kw* (always formatted: ``{{`` is a brace)."""
    return _t(key, _formatted=True, **kw)


__all__ = ["SYSTEM_DIR", "STRINGS", "has", "load_blocks", "missing_keys", "prompt_text"]
