"""THE one persona setting: the owner's choice of the voice every NEW chat uses.

One store, one reader, every seat:

* the store is the owner's ``session.persona`` preference (``core.prefs``) — the
  same key the agent's guarded ``preferences`` proposal writes and the owner
  approves;
* the reader is :func:`agents.personality.persona_resolver.resolve_persona`
  (every session: chat seats, the REPL, cron and goal runs, A2A), which ranks it
  above the operator's persona and the default character;
* every owner seat (Telegram and the other chat apps, the REPL) sets it through
  :func:`persona_command` / :func:`set_persona`. A seat renders; it holds no rule.

The value is a selector in the ONE order: a template key, else a character slug,
else literal text (``persona_resolver.resolve_persona_selector``). Literal text is
threat-scanned when it is written (``core.prefs.write_preference``) AND when it is
read here: a hand-edited file never reaches the prompt unscanned.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

PREF_KEY = "session.persona"
#: ``/persona reset`` (or one of its twins) removes the owner's choice.
RESET_WORDS = ("reset", "default", "clear")
#: ``/persona list`` reads the names (a seat with tables draws them); never saved.
LIST_WORDS = ("list", "ls")


def scope(user_id: Optional[str] = None, home_dir=None) -> Tuple[Optional[str], str]:
    """``(tenant, prefs home)`` the setting lives in: the owner principal and the
    identity home unless a caller names its own (a test, a REPL bound to a profile)."""
    if user_id and home_dir:
        return str(user_id), str(home_dir)
    from core.prefs import owner_pref_scope
    owner, home = owner_pref_scope()
    return (str(user_id) if user_id else owner), (str(home_dir) if home_dir else home)


def owner_selector(user_id: Optional[str] = None, home_dir=None) -> Tuple[str, str]:
    """``(selector, problem)``. ``selector`` is ``""`` when the owner chose nothing.
    ``problem`` names why a stored value is NOT used (it failed the identity scan,
    or the scan could not run); the caller then falls back to the operator's
    persona. Never raises."""
    uid, home = scope(user_id, home_dir)
    if not (uid and home):
        return "", ""
    try:
        from core.prefs import resolve
        value = resolve("session.persona", uid, home)  # the literal key: the dead-key ratchet reads it
    except Exception as e:  # noqa: BLE001 — an unreadable store: say so, use the fallback
        return "", f"the persona setting could not be read ({type(e).__name__}: {e})"
    value = str(value or "").strip()
    if not value:
        return "", ""
    try:
        from core.security.threat_scan import is_identity_suspicious
        flagged = is_identity_suspicious(value)
    except Exception as e:  # noqa: BLE001 — fail CLOSED
        logger.warning("persona setting scan error (%s): the setting is not used", e)
        return "", "the persona setting could not be safety-scanned, so it is not used"
    if flagged:
        logger.warning("persona setting failed the identity scan: it is not used")
        return "", "the persona setting failed the safety scan, so it is not used"
    return value, ""


def set_persona(value: str, *, user_id: Optional[str] = None,
                home_dir=None) -> Tuple[bool, str]:
    """Write the owner's choice. ``(ok, what was saved | why not)``."""
    value = (value or "").strip()
    if not value:
        return False, "Name a persona: /persona <name>."
    from agents.task.templates import TEMPLATES
    if value.lower() in TEMPLATES:
        value = value.lower()
    uid, home = scope(user_id, home_dir)
    if not (uid and home):
        return False, "the owner's settings could not be located"
    from core.prefs import write_preference
    ok, err = write_preference(home, uid, PREF_KEY, value)
    if not ok:
        return False, err
    kind, _ = classify(value)
    return True, {"template": f"template '{value}'",
                  "character": f"character '{value}'"}.get(
        kind, f"your own text '{value[:60]}{'…' if len(value) > 60 else ''}'")


def clear_persona(*, user_id: Optional[str] = None, home_dir=None) -> Tuple[bool, str]:
    """Remove the owner's choice; the operator's persona applies again."""
    uid, home = scope(user_id, home_dir)
    if not (uid and home):
        return False, "the owner's settings could not be located"
    from core.prefs import (_CACHE, _parse_flat, _render_toml, load_preferences,
                            preferences_path)
    from core.prefs_io import REMOVE, locked_write
    path = preferences_path(home, uid)
    if path is None:
        return False, "the owner's settings could not be located"
    try:
        stored = load_preferences(home, uid, strict=True)
    except Exception as e:  # noqa: BLE001 — PrefsUnreadable: never "it is not set"
        return False, str(e)
    if PREF_KEY not in stored:
        return True, "nothing was set"
    ok, err = locked_write(path, _parse_flat, PREF_KEY, REMOVE, _render_toml, _CACHE)
    if not ok:
        return False, err
    return True, "removed"


def classify(value: Optional[str]) -> Tuple[str, str]:
    """``(kind, name)``: ``template`` / ``character`` / ``literal`` / ``none``."""
    val = (value or "").strip()
    if not val:
        return "none", ""
    from agents.task.templates import TEMPLATES
    if val in TEMPLATES:
        return "template", val
    from agents.personality.persona_resolver import find_character_file
    if find_character_file(val) is not None:
        return "character", val
    return "literal", val


def persona_names(characters_dir: Optional[Path] = None) -> Tuple[List[str], List[str]]:
    """``(character slugs, unreadable directories)`` over every tier of the ONE
    search order (or one directory a test names). A directory that exists but
    cannot be listed is NAMED, never read as empty."""
    from agents.personality.persona_resolver import character_search_dirs
    dirs = [Path(characters_dir)] if characters_dir is not None else character_search_dirs()
    names, unreadable = set(), []
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            entries = os.listdir(d)
        except OSError as e:
            unreadable.append(f"{d} ({e.strerror or type(e).__name__})")
            continue
        names.update(n[: -len(".character.json")] for n in entries
                     if n.endswith(".character.json"))
    return sorted(names), unreadable


def persona_command(args=(), *, user_id: Optional[str] = None, home_dir=None) -> str:
    """``/persona`` on an owner chat seat. No argument: the report. ``reset``: back
    to the operator's persona. Anything else: the owner's choice for new chats."""
    from agents.personality.persona_report import persona_report
    words = [str(a) for a in (args or ()) if str(a).strip()]
    if not words or (len(words) == 1 and words[0].lower() in LIST_WORDS):
        return persona_report(user_id=user_id, home_dir=home_dir)
    value = " ".join(words).strip()
    if value.lower() in RESET_WORDS:
        ok, what = clear_persona(user_id=user_id, home_dir=home_dir)
        if not ok:
            return f"❌ Persona not reset: {what}. Nothing changed."
        return ("✅ Persona: back to the default — "
                + ("it was already the default." if what == "nothing was set"
                   else "it applies from the next session.")
                + " This chat's persona is unchanged until /new.")
    ok, what = set_persona(value, user_id=user_id, home_dir=home_dir)
    if not ok:
        return f"❌ Persona not saved: {what}. Nothing changed."
    note = ""
    if classify(value)[0] == "literal":
        note = ("\nIt is not a template or a character name, so the text itself is the "
                "persona. `/persona` lists the names.")
    return (f"✅ Persona saved: {what}. It applies from the next session, on every seat; "
            f"this chat's persona is unchanged until /new.{note}")
