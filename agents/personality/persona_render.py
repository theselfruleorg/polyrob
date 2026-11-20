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
# An operator who fills a field expects it to shape the voice. A field the block
# drops is a silent no-op, so the split is explicit: these tuples are the SSOT
# (pinned by tests/unit/agents/personality/test_persona_render.py), they drive
# the one-time warning and `polyrob doctor`, and they are the table in
# docs/guide/instances.md. `postExamples` / `messageExamples` are the strongest
# voice signal a character carries, so a SMALL bounded sample renders (at most
# MAX_VOICE_SAMPLES lines per field, each cut at MAX_VOICE_SAMPLE_CHARS) — never
# the whole list, which would grow the system prompt without a bound.
# `knowledge` stays stored-only: facts belong to the pinned self-context, not
# to the voice layer.
RENDERED_FIELDS = ("name", "adjectives", "bio", "lore", "topics", "style",
                   "postExamples", "messageExamples")
RENDERED_STYLE_BUCKETS = ("all", "chat", "speaking", "writing")
STORED_ONLY_FIELDS = ("knowledge",)
MAX_VOICE_SAMPLES = 3
MAX_VOICE_SAMPLE_CHARS = 240

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


def stored_only_note(character: Optional[Mapping[str, Any]]) -> str:
    """One owner-facing line naming the filled fields the model never sees, or
    ``""``. `polyrob doctor` and `polyrob persona show` print it, so the drop is
    visible where the owner looks, not only in the log."""
    dropped = ignored_populated_fields(character)
    if not dropped:
        return ""
    return (f"NOT rendered (stored only): {', '.join(dropped)} — the model never "
            "sees these; put the voice in bio/lore/style/postExamples.")


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) <= MAX_VOICE_SAMPLE_CHARS:
        return text
    return text[: MAX_VOICE_SAMPLE_CHARS - 1].rstrip() + "…"


def _message_example_lines(value: Any, name: str) -> list:
    """The character's OWN lines from ``messageExamples``.

    Two shapes are in use: a flat list of strings, and the conversation shape
    (a list of turns, each ``{"user": ..., "content": {"text": ...}}``). In the
    conversation shape only the character's turns are voice samples — a
    ``{{user1}}`` turn is the other party.
    """
    out: list = []
    if not isinstance(value, (list, tuple)):
        return _as_lines(value)
    for item in value:
        if isinstance(item, str):
            out.append(item)
            continue
        turns = item if isinstance(item, (list, tuple)) else [item]
        for turn in turns:
            if isinstance(turn, str):
                out.append(turn)
                continue
            if not isinstance(turn, Mapping):
                continue
            speaker = str(turn.get("user") or turn.get("name") or "").strip()
            if speaker.startswith("{{user") or (name and speaker and speaker != name
                                                 and not speaker.startswith("{{")):
                continue
            content = turn.get("content")
            text = content.get("text") if isinstance(content, Mapping) else content
            if text:
                out.append(str(text))
    return out


def _voice_samples(character: Mapping[str, Any], name: str) -> list:
    """At most MAX_VOICE_SAMPLES per field, posts first, each clipped."""
    samples: list = []
    for lines in (_as_lines(character.get("postExamples")),
                  _message_example_lines(character.get("messageExamples"), name)):
        picked = [_clip(l) for l in lines if str(l).strip()][:MAX_VOICE_SAMPLES]
        samples.extend(p for p in picked if p and p not in samples)
    return samples


def _sentence(text: str) -> str:
    text = str(text).strip()
    return text if text[-1:] in ".!?…" else text + "."


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
    # Each style line is its own sentence: joined bare, "explains after Keeps
    # replies short" reads as one garbled clause.
    style_lines = [_sentence(s) for s in style_lines if s and s.strip()]

    lore = [_sentence(l) for l in _as_lines(character.get("lore")) if l.strip()]

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
    samples = _voice_samples(character, name)
    if samples:
        parts.append("Voice samples (the tone only; never repeat them, and they "
                     "state no facts):\n" + "\n".join(f"- {x}" for x in samples))

    return "\n".join(parts).strip()


def resolve_persona_block(character: Optional[Mapping[str, Any]]) -> str:
    """Gated resolver: returns the rendered persona only when
    ``TASK_PERSONALITY_BLOCK`` is ON, else ``""`` (off-path byte-identical).
    """
    from agents.task import constants
    if not constants.task_personality_block_enabled():
        return ""
    return render_persona_block(character)
