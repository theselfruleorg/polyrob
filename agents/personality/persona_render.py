"""Pure persona renderer (S1, chat consolidation).

Turns a character dict (the output of ``Character.to_dict()``) into a terse,
deterministic plain-text persona block for injection into the unified Task
agent's ``<identity>`` section. Kept PURE and dependency-free on purpose: the
task-agent core must NOT import ``Character``/``CharacterManager``/the chat
stack — it only ever receives a ``str``. The chat front door renders the
shared (surviving) CharacterManager's default character to text at the call
boundary and hands that string in.
"""
import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# --- the rendered/stored-only split (F5) -----------------------------------
# An operator writes `postExamples` and `messageExamples` — the two fields that
# most obviously teach a voice — and gets NOTHING: they are parsed, stored on
# Character and populated by every shipped preset, but no path consumes them.
# The silent no-op on an authored field is the defect. These two tuples are the
# SSOT for the split (pinned by tests/unit/agents/personality/test_persona_render.py),
# they drive the one-time warning below, and they are the table in
# docs/guide/instances.md. Folding the stored-only fields INTO the block is a
# product call, deliberately not taken here.
RENDERED_FIELDS = ("name", "adjectives", "bio", "lore", "topics", "style")
RENDERED_STYLE_BUCKETS = ("all", "chat", "speaking")
STORED_ONLY_FIELDS = ("knowledge", "messageExamples", "postExamples", "style.writing")

# (character name, dropped fields) already warned about — the renderer runs per
# session, and this must never become per-turn noise.
_warned_ignored_fields: set = set()


def ignored_populated_fields(character: Optional[Mapping[str, Any]]) -> list:
    """The STORED_ONLY_FIELDS this character populates but the block drops."""
    if not character or not isinstance(character, Mapping):
        return []
    found = []
    for field in STORED_ONLY_FIELDS:
        if "." in field:
            parent, child = field.split(".", 1)
            container = character.get(parent) or {}
            value = container.get(child) if isinstance(container, Mapping) else None
        else:
            value = character.get(field)
        if value:
            found.append(field)
    return found


def _warn_ignored_fields(character: Mapping[str, Any]) -> None:
    """Say ONCE, per character, which authored fields never reach the model."""
    dropped = ignored_populated_fields(character)
    if not dropped:
        return
    key = (str(character.get("name") or ""), tuple(dropped))
    if key in _warned_ignored_fields:
        return
    _warned_ignored_fields.add(key)
    logger.warning(
        "character %r populates %s, which the persona block does NOT render "
        "(rendered fields: %s + style.%s). Those values are stored but never "
        "reach the model — move the voice you want into bio/lore/style.",
        key[0] or "<unnamed>", ", ".join(dropped),
        ", ".join(f for f in RENDERED_FIELDS if f != "style"),
        "/style.".join(RENDERED_STYLE_BUCKETS),
    )


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

    # Log-only; the returned block is byte-identical either way (F5).
    try:
        _warn_ignored_fields(character)
    except Exception:  # pragma: no cover - a warning must never break a session
        pass

    name = str(character.get("name") or "").strip()
    bio = " ".join(_as_lines(character.get("bio"))).strip()
    adjectives = [a.strip() for a in _as_lines(character.get("adjectives")) if a.strip()]
    topics = [t.strip() for t in _as_lines(character.get("topics")) if t.strip()]

    style = character.get("style") or {}
    style_lines: list = []
    if isinstance(style, Mapping):
        for key in RENDERED_STYLE_BUCKETS:
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
