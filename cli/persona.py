"""Persona resolution for CLI surfaces (REPL + one-shot run).

``resolve_cli_persona()`` is the single helper used by both ``polyrob run``
(one-shot) and the interactive REPL so the init-chosen persona is wired
consistently into every CLI agent path.
"""
import logging
from typing import Optional
from core.runtime_paths import data_dir_or_home

logger = logging.getLogger(__name__)


def resolve_cli_persona(user_id: Optional[str] = None, home_dir=None) -> str:
    """Persona text for the CLI agent's <identity>, or "" when the gate is off.

    T1-07: the surface-shared ``agents.personality.persona_resolver`` renders it,
    so the CLI and every chat seat speak with the SAME voice: the owner's persona
    setting (``agents/personality/persona_setting.py``, the ``session.persona``
    preference ``/persona <name>`` writes on every seat; threat-scanned on read)
    > the operator's POLYROB_PERSONA > the default character > "".

    ``user_id``/``home_dir`` name the setting's scope (a profile, a test); the
    default is the owner's, the scope every chat session reads.
    """
    from agents.personality.persona_resolver import resolve_persona_sync
    # The resolver fails open: an unreadable setting falls back to the operator's persona.
    return resolve_persona_sync(
        user_id=user_id, home_dir=data_dir_or_home(home_dir) if home_dir else None)


def cli_gate_on(env=None) -> bool:
    """``TASK_PERSONALITY_BLOCK`` as `polyrob run` / the REPL will resolve it.

    A standalone `polyrob persona`/`polyrob doctor` process never runs
    ``build_cli_container``, so it never sees that function's
    ``os.environ.setdefault("POLYROB_LOCAL", "1")``. Reading the gate raw there
    reports "persona: off" over a persona that IS live in every session — the
    confident-and-wrong shape F13 exists to kill. So an ABSENT ``POLYROB_LOCAL``
    means ON here, exactly as ``doctor.local_flag_on(absent_means_on=True)``
    already resolves it; an explicit value of either flag always wins.
    """
    import os

    from core.config_policy import _FALSEY
    env = os.environ if env is None else env
    explicit = (env.get("TASK_PERSONALITY_BLOCK") or "").strip()
    if explicit:
        return explicit.lower() not in _FALSEY
    raw = env.get("POLYROB_LOCAL")
    if raw is None:
        return True
    return str(raw).strip().lower() not in _FALSEY


def classify_persona_selector(value: Optional[str]) -> tuple:
    """``(kind, name)`` for a raw selector, using the ONE resolution order.

    ``kind`` is ``"template"``, ``"character"`` or ``"literal"``; ``name`` is the
    template key / character slug, or the literal text itself. ``("none", "")``
    for an empty selector. Pure classification — no rendering.
    """
    val = (value or "").strip()
    if not val:
        return ("none", "")
    try:
        from agents.task.templates import TEMPLATES
        if val in TEMPLATES:
            return ("template", val)
    except Exception:
        pass
    try:
        from agents.personality.persona_resolver import find_character_file
        if find_character_file(val) is not None:
            return ("character", val)
    except Exception:
        pass
    return ("literal", val)


def describe_active_persona(user_id: Optional[str] = None, home_dir=None,
                           *, gate: Optional[bool] = None) -> dict:
    """What persona is ACTUALLY live, and where it comes from (F13).

    Nothing reported which persona was in effect — not the banner, ``/persona``,
    ``/self`` or ``polyrob doctor`` — so a wrong pref (F1), a dropped field (F5)
    or an absent character file (F12) were all invisible at once. This is the ONE
    describer those surfaces render.

    Keys: ``gate`` (bool), ``kind`` (``off``/``template``/``character``/
    ``literal``), ``name``, ``source`` (``pref``/``env``/``default``) and
    ``path`` (the resolved character file, when the character tier is live).
    Never raises.
    """
    info = {"gate": False, "kind": "off", "name": "", "source": "", "path": None}
    if gate is None:
        try:
            from core.config_policy import task_personality_block_enabled
            gate = task_personality_block_enabled()
        except Exception:
            return info
    if not gate:
        return info
    info["gate"] = True

    selector, source = "", ""
    try:
        from agents.personality.persona_setting import owner_selector
        pref_persona, _problem = owner_selector(
            user_id, data_dir_or_home(home_dir) if home_dir else None)
        if pref_persona:
            selector, source = pref_persona, "pref"
    except Exception:
        pass
    if not selector:
        import os
        env_persona = (os.environ.get("POLYROB_PERSONA") or "").strip()
        if env_persona:
            selector, source = env_persona, "env"

    if selector:
        kind, name = classify_persona_selector(selector)
        info.update(kind=kind, name=name, source=source)
        if kind == "character":
            try:
                from agents.personality.persona_resolver import find_character_file
                found = find_character_file(name)
                info["path"] = str(found) if found else None
            except Exception:
                pass
        return info

    try:
        from agents.personality.persona_resolver import active_character
        name, path = active_character()
        info.update(kind="character", name=name, source="default",
                    path=str(path) if path else None)
    except Exception:
        pass
    return info


def active_persona_line(user_id: Optional[str] = None, home_dir=None,
                        *, gate: Optional[bool] = None) -> str:
    """One legible line for ``polyrob doctor --full`` (F13; 043 A13 fix round
    1: this line renders inside ``setup_lines()``, part of the check
    transcript that moved behind ``--full``). Never raises."""
    info = describe_active_persona(user_id, home_dir, gate=gate)
    if not info["gate"]:
        return ("persona: off (TASK_PERSONALITY_BLOCK is off — no <identity> "
                "persona block is injected)")
    kind, name, source = info["kind"], info["name"], info["source"]
    if kind == "character":
        where = info["path"] or "NOT FOUND — falling back to the packaged neutral character"
        via = {"pref": "session.persona pref", "env": "POLYROB_PERSONA",
               "default": "PERSONALITY_DEFAULT_CHARACTER"}.get(source, source)
        line = f"persona: character '{name}' via {via} ({where})"
        try:
            from agents.personality.persona_render import stored_only_note
            from agents.personality.persona_resolver import load_character_dict
            note = stored_only_note(load_character_dict(name)) if info["path"] else ""
        except Exception:
            note = ""
        return f"{line}\n  {note}" if note else line
    if kind == "template":
        return f"persona: template '{name}' via {source or 'env'}"
    if kind == "literal":
        return (f"persona: literal text via {source or 'env'} "
                f"({len(name)} chars) — no character is active")
    return "persona: none"


def _all_character_names() -> list:
    """Every character slug across every tier of the ONE search order."""
    try:
        from agents.personality.persona_resolver import character_search_dirs
        seen = set()
        for d in character_search_dirs():
            try:
                for f in d.glob("*.character.json"):
                    seen.add(f.stem.removesuffix(".character"))
            except Exception:
                continue
        return sorted(seen)
    except Exception:
        return []


def build_persona_listing(user_id: Optional[str] = None, home_dir=None,
                          character_names: Optional[list] = None,
                          *, gate: Optional[bool] = None) -> dict:
    """THE ``/persona`` + ``polyrob persona list`` listing (F2/F3/F13).

    One builder so the REPL command and the CLI command can never drift into
    two different pictures of the same namespaces. Returns::

        {"templates": [[label, description], ...],
         "characters": [[label, bio], ...],
         "guidance": [str, ...],
         "active": <describe_active_persona() dict>}

    A label carries a trailing ``← active`` marker for the live selector.
    """
    from agents.personality.persona_resolver import character_bio
    from agents.task.templates import TEMPLATES

    active = describe_active_persona(user_id, home_dir, gate=gate)

    def mark(kind: str, name: str) -> str:
        return "  ← active" if (kind == active["kind"] and name == active["name"]) else ""

    names: list = list(character_names) if character_names is not None else _all_character_names()

    guidance = [
        "/persona <name> sets the persona of every new chat, on every seat: a template "
        "key, else a character name, else your own text.",
        "/persona reset goes back to the default. The change applies to the NEXT session. "
        "`polyrob persona show` prints the rendered block.",
    ]
    if active["gate"]:
        where = f" ({active['path']})" if active.get("path") else ""
        via = {"pref": "your /persona setting", "env": "the operator's setting",
               "default": "the default"}.get(active["source"], active["source"])
        guidance.insert(0, f"active: {active['kind']} {active['name']!r}"
                           f" via {via}{where}")
    else:
        guidance.insert(0, "active: none — the operator's setting turns the persona "
                           "block off, so no persona reaches the model")

    return {
        "templates": [[f"{k}{mark('template', k)}", "built-in operating template"]
                      for k in sorted(TEMPLATES)],
        "characters": [[f"{p}{mark('character', p)}", character_bio(p)[:80]]
                       for p in names],
        "guidance": guidance,
        "active": active,
    }
