"""Pure persona renderer (S1, chat consolidation).

Turns a character dict (the output of ``Character.to_dict()``) into a terse,
deterministic plain-text persona block for injection into the unified Task
agent's ``<identity>`` section. Kept PURE and dependency-free on purpose: the
task-agent core must NOT import ``Character``/``CharacterManager``/the chat
stack — it only ever receives a ``str``. The chat front door renders the
shared (surviving) CharacterManager's default character to text at the call
boundary and hands that string in.
"""
from typing import Any, Mapping, Optional


def _as_lines(value: Any) -> list:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [str(value)]


def render_persona_block(character: Optional[Mapping[str, Any]]) -> str:
    """Render a character mapping to a terse persona block.

    Accepts the dict produced by ``Character.to_dict()`` (or any mapping with
    ``name``/``bio``/``adjectives``/``style``/``topics``/``lore`` keys). Returns
    ``""`` for a falsy/empty character so the off-path stays byte-identical.
    """
    if not character or not isinstance(character, Mapping):
        return ""

    name = str(character.get("name") or "").strip()
    bio = " ".join(_as_lines(character.get("bio"))).strip()
    adjectives = [a.strip() for a in _as_lines(character.get("adjectives")) if a.strip()]
    topics = [t.strip() for t in _as_lines(character.get("topics")) if t.strip()]

    style = character.get("style") or {}
    style_lines: list = []
    if isinstance(style, Mapping):
        for key in ("all", "chat", "speaking"):
            style_lines.extend(_as_lines(style.get(key)))
    else:
        style_lines.extend(_as_lines(style))
    style_lines = [s.strip() for s in style_lines if s and s.strip()]

    lore = [l.strip() for l in _as_lines(character.get("lore")) if l.strip()]

    parts: list = []
    if name:
        header = f"You are {name}."
        if adjectives:
            header = f"You are {name} — {', '.join(adjectives)}."
        parts.append(header)
    elif adjectives:
        parts.append(f"You are {', '.join(adjectives)}.")

    if bio:
        parts.append(bio)
    if lore:
        parts.append("Background: " + " ".join(lore))
    if topics:
        parts.append("You focus on: " + ", ".join(topics) + ".")
    if style_lines:
        parts.append("Style: " + " ".join(style_lines))

    return "\n".join(parts).strip()


def resolve_persona_block(character: Optional[Mapping[str, Any]]) -> str:
    """Gated resolver: returns the rendered persona only when
    ``TASK_PERSONALITY_BLOCK`` is ON, else ``""`` (off-path byte-identical).
    """
    from agents.task import constants
    if not constants.task_personality_block_enabled():
        return ""
    return render_persona_block(character)
