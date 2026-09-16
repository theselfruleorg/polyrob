"""One persona resolver for every surface (T1-07, 2026-07-06 structural review).

Before this module, two live persona SSOTs were split by surface: the chat_once
path rendered the default character JSON (via CharacterManager +
render_persona_block) while the CLI rendered the ``templates.py`` persona for
``POLYROB_PERSONA`` — same instance, contradictory voice, and template-only
guidance (e.g. trading's "Never executes trades") shipped on one surface only.

Single precedence, applied identically everywhere:

1. Gate: ``task_personality_block_enabled()`` off -> ``""``.
2. Explicit ``POLYROB_PERSONA``, through :func:`resolve_persona_selector`: a
   known template key renders that template's persona; a known CHARACTER slug
   renders that character (F1 — the selector namespace ``/persona`` lists);
   any other non-empty value is used as LITERAL free-form persona text.
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
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The neutral framework persona shipped in the package. A specific bot identity
# (e.g. an operator's own character) is data, not framework code: it lives in
# <data_dir>/characters or a profile, selected via PERSONALITY_DEFAULT_CHARACTER.
DEFAULT_CHARACTER_NAME = "polyrob"

_warned_missing_character = False


def _gate_on() -> bool:
    try:
        from agents.task.constants import task_personality_block_enabled
        return task_personality_block_enabled()
    except Exception:
        return False


def resolve_persona_selector(val: Optional[str]) -> Optional[str]:
    """THE one selector->persona-text order, shared by every surface (F1).

    ``template key`` -> ``character slug`` -> ``literal free-form text``.

    Before this existed, the CLI's ``/persona`` listed CHARACTER slugs and then
    persisted whatever it was given as literal text unless it was a TEMPLATE
    key — so picking a name the command had just printed replaced the whole
    ``<identity>`` block with that single word and reported success.

    The character branch reaches the SAME operator-authored file tier that
    ``PERSONALITY_DEFAULT_CHARACTER`` selects (``character_search_dirs``), by a
    different selector — it does not widen the free-text threat-scan surface,
    which still guards the literal branch at its own write/load sites.

    Returns ``None`` for an empty selector.
    """
    val = (val or "").strip()
    if not val:
        return None
    try:
        from agents.task.templates import TEMPLATES, resolve_template_persona
        if val in TEMPLATES:
            return resolve_template_persona(val)
    except Exception:
        pass
    character = character_persona_text(val)
    if character:
        return character
    # Neither a template key nor a known character: LITERAL persona text
    # (free-form), rather than silently degrading to the "general" template.
    return val


def _explicit_persona() -> Optional[str]:
    """Tier 2: the operator-chosen POLYROB_PERSONA (template/character/literal)."""
    return resolve_persona_selector(os.environ.get("POLYROB_PERSONA"))


def character_search_dirs(data_dir: Optional[str] = None) -> "list[Path]":
    """Character-set directories in precedence order (highest first):

    1. ``<data_dir>/characters`` — this run's data home;
    2. ``<config home>/characters`` — the profile root (a profile ships its
       characters beside its ``.env``);
    3. ``<install root>/data/characters`` — the repo-shipped use-case personas
       (researcher/coder/analyst/…), anchored to the CODE tree, never the cwd;
    4. the packaged neutral set.

    THE one list — the CharacterManager, the persona resolver, and the CLI
    ``/persona`` listing all derive from it.
    """
    dirs: "list[Path]" = []
    if not data_dir:  # None/empty both resolve via the data-home SSOT
        try:
            from core.runtime_paths import data_dir_or_home
            data_dir = data_dir_or_home((os.environ.get("POLYROB_DATA_DIR") or "").strip())
        except Exception:
            data_dir = None
    if data_dir:
        dirs.append(Path(data_dir) / "characters")
    try:
        from core.paths import polyrob_home
        dirs.append(polyrob_home() / "characters")
    except Exception:
        pass
    # agents/personality/ -> agents/ -> the install/code root.
    dirs.append(Path(__file__).parents[2] / "data" / "characters")
    dirs.append(Path(__file__).parent / "characters")
    return dirs


def resolve_characters_dir(data_dir: Optional[str] = None) -> Path:
    """The FIRST search dir that actually holds ``*.character.json`` (else the
    packaged set). Single-dir consumers (CharacterManager) use this."""
    dirs = character_search_dirs(data_dir)
    for d in dirs[:-1]:
        try:
            if d.exists() and list(d.glob("*.character.json")):
                return d
        except Exception:
            continue
    return dirs[-1]


# A character slug is ONE filename component, never a path. Both selectors that
# reach the filesystem with it (PERSONALITY_DEFAULT_CHARACTER and, since F1, a
# /persona argument or POLYROB_PERSONA value) run through this.
_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def is_safe_character_slug(name: Optional[str]) -> bool:
    """Whether *name* may be used as a ``<slug>.character.json`` filename."""
    name = (name or "").strip()
    if not name or name in {".", ".."} or not _SAFE_SLUG_RE.match(name):
        return False
    return "/" not in name and os.sep not in name


def find_character_file(name: str) -> Optional[Path]:
    """The named character searched across ALL tiers (highest wins), or None."""
    if not is_safe_character_slug(name):
        return None
    for d in character_search_dirs():
        try:
            f = d / f"{name.strip()}.character.json"
            if f.is_file():
                return f
        except Exception:
            continue
    return None


# Back-compat alias for the private name this module used before F1.
_find_character_file = find_character_file


def load_character_dict(name: str) -> Optional[dict]:
    """Parse the named character, or None (unknown slug / unreadable / bad JSON)."""
    f = find_character_file(name)
    if f is None:
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"character {name!r} read skipped: {e}")
        return None
    return data if isinstance(data, dict) else None


def character_persona_text(name: str) -> Optional[str]:
    """The rendered persona block for a known character slug, else None."""
    data = load_character_dict(name)
    if not data:
        return None
    from agents.personality.persona_render import render_persona_block
    return render_persona_block(data) or None


def character_bio(name: str) -> str:
    """The character's bio as one line (``""`` when unknown/absent) — the
    ``/persona`` listing's bio column reads THIS, not a cwd-relative guess."""
    data = load_character_dict(name) or {}
    raw = data.get("bio") or ""
    text = raw if isinstance(raw, str) else " ".join(str(x) for x in raw if x)
    return " ".join(text.split())


def active_character() -> "tuple[str, Optional[Path]]":
    """``(name, resolved file or None)`` for the character tier — the file the
    default-character path would actually read (F13)."""
    name = (os.environ.get("PERSONALITY_DEFAULT_CHARACTER")
            or DEFAULT_CHARACTER_NAME).strip() or DEFAULT_CHARACTER_NAME
    found = find_character_file(name)
    if found is None:
        found = find_character_file(DEFAULT_CHARACTER_NAME)
    return name, found


def _default_character_dict() -> Optional[dict]:
    """Tier 3 (sync): read the default character JSON directly, fail-open.

    Fallback shim (W1, neutral-identity): when the named character does not
    resolve (e.g. an install upgraded past the packaged ``rob`` character, or a
    data-dir character set lacks the named file), degrade to the packaged
    neutral ``polyrob`` character with a one-shot warning — an existing install
    must never hard-fail or silently lose its persona block on upgrade.
    """
    global _warned_missing_character
    name = (os.environ.get("PERSONALITY_DEFAULT_CHARACTER")
            or DEFAULT_CHARACTER_NAME).strip() or DEFAULT_CHARACTER_NAME
    try:
        char_file = _find_character_file(name)
        if char_file is not None:
            return json.loads(char_file.read_text(encoding="utf-8"))
        fallback = Path(__file__).parent / "characters" / (
            f"{DEFAULT_CHARACTER_NAME}.character.json")
        if fallback.is_file():
            if not _warned_missing_character:
                logger.warning(
                    "default character '%s' not found in any characters dir; "
                    "falling back to the neutral packaged '%s' character (set "
                    "PERSONALITY_DEFAULT_CHARACTER or drop the file in "
                    "<data_dir>/characters/ to restore it)",
                    name, DEFAULT_CHARACTER_NAME)
                _warned_missing_character = True
            return json.loads(fallback.read_text(encoding="utf-8"))
        return None
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
