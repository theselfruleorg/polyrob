"""One persona resolver for every surface (T1-07, 2026-07-06 structural review).

Before this module, two live persona SSOTs were split by surface: the chat_once
path rendered the default character JSON (via CharacterManager +
render_persona_block) while the CLI rendered the ``templates.py`` persona for
``POLYROB_PERSONA`` — same instance, contradictory voice, and template-only
guidance (e.g. trading's "Never executes trades") shipped on one surface only.

Single precedence, applied identically everywhere:

1. Gate: ``task_personality_block_enabled()`` off -> ``""``.
2. Explicit ``POLYROB_PERSONA``: a known template key renders that template's
   persona; any other non-empty value is used as LITERAL free-form persona text.
3. The default character, rendered by the pure ``render_persona_block``:
   - async path (:func:`resolve_persona`): the container's ``character_manager``;
   - sync path (:func:`resolve_persona_sync`): the default character JSON read
     directly, mirroring CharacterManager's directory precedence
     (``<data_dir>/characters`` when it holds ``*.character.json``, else the
     package ``agents/personality/characters``).
4. ``""`` — byte-identical off-path.

Note the pinned SELF-CONTEXT stays authoritative over any persona text
(<identity>/<source-precedence>, T1-08); persona only styles the voice.
"""
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _gate_on() -> bool:
    try:
        from agents.task.constants import task_personality_block_enabled
        return task_personality_block_enabled()
    except Exception:
        return False


def _explicit_persona() -> Optional[str]:
    """Tier 2: the operator-chosen POLYROB_PERSONA (template key or literal)."""
    val = (os.environ.get("POLYROB_PERSONA") or "").strip()
    if not val:
        return None
    try:
        from agents.task.templates import TEMPLATES, resolve_template_persona
        if val in TEMPLATES:
            return resolve_template_persona(val)
    except Exception:
        pass
    # A non-template value is LITERAL persona text (free-form), rather than
    # silently degrading to the "general" template.
    return val


def _characters_dir() -> Path:
    """Mirror CharacterManager's precedence without requiring the container."""
    data_dir = (os.environ.get("POLYROB_DATA_DIR") or "data").strip() or "data"
    data_chars = Path(data_dir) / "characters"
    try:
        if data_chars.exists() and list(data_chars.glob("*.character.json")):
            return data_chars
    except Exception:
        pass
    return Path(__file__).parent / "characters"


def _default_character_dict() -> Optional[dict]:
    """Tier 3 (sync): read the default character JSON directly, fail-open."""
    name = (os.environ.get("PERSONALITY_DEFAULT_CHARACTER") or "rob").strip() or "rob"
    char_file = _characters_dir() / f"{name}.character.json"
    try:
        if not char_file.is_file():
            return None
        return json.loads(char_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"default character read skipped: {e}")
        return None


def resolve_persona_sync() -> str:
    """Persona text for surfaces without an initialized container (CLI/REPL)."""
    if not _gate_on():
        return ""
    explicit = _explicit_persona()
    if explicit is not None:
        return explicit
    from agents.personality.persona_render import render_persona_block
    return render_persona_block(_default_character_dict())


async def resolve_persona(container=None) -> str:
    """Persona text for async surfaces (chat front door). Same precedence; the
    character tier prefers the container's CharacterManager (honors config-driven
    default-character selection), falling back to the direct file read."""
    if not _gate_on():
        return ""
    explicit = _explicit_persona()
    if explicit is not None:
        return explicit
    from agents.personality.persona_render import render_persona_block
    char_dict = None
    try:
        cm = container.get_service("character_manager") if container else None
        if cm is not None and hasattr(cm, "get_default_character"):
            character = await cm.get_default_character()
            if character:
                char_dict = character.to_dict() if hasattr(character, "to_dict") else character
    except Exception as e:
        logger.debug(f"character_manager persona resolve skipped: {e}")
    if char_dict is None:
        char_dict = _default_character_dict()
    return render_persona_block(char_dict)
