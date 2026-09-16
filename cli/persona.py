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

    T1-07: delegates to the surface-shared ``agents.personality.persona_resolver``
    so CLI and chat render the SAME voice: explicit POLYROB_PERSONA (template key
    or literal free-form text) > the default character > "".

    owner-UX P1 T5: when ``user_id`` is given, a ``session.persona`` preference
    (override merge — pref > env > default) may replace the default persona
    SOURCE for a new session, resolved through the SAME template-key-or-literal
    precedence the resolver applies to ``POLYROB_PERSONA``. The resolver itself
    stays pure/unaware of prefs; this wiring lives entirely here, at the call
    site. No ``user_id`` or no pref file => byte-identical to
    ``resolve_persona_sync()``.
    """
    from agents.personality.persona_resolver import resolve_persona_sync
    if not user_id:
        return resolve_persona_sync()
    try:
        from agents.task.constants import task_personality_block_enabled
        if not task_personality_block_enabled():
            return ""
        import os
        from core.prefs import resolve_with_source
        env_persona = (os.environ.get("POLYROB_PERSONA") or "").strip() or None
        pref_persona, source = resolve_with_source(
            "session.persona", user_id, data_dir_or_home(home_dir),
            env_value=env_persona, default=None,
        )
        if source == "pref" and pref_persona:
            # owner-UX P1 final review (item 2b): load-side backstop scan. The
            # write path (core.prefs.write_preference) already threat-scans a
            # NEW session.persona pref, but a hand-edited preferences.toml (or
            # a pref written before that scan existed) bypasses it — never
            # inject unscanned free text as the session's <identity> source.
            # Fail-CLOSED: a hit OR a scanner error both fall back to the
            # pre-pref persona resolution (pref ignored, never crash).
            try:
                from modules.memory.task.threat_scan import is_identity_suspicious
                flagged = is_identity_suspicious(pref_persona)
            except Exception as e:
                logger.warning(
                    "session.persona pref scan error (%s) — ignoring pref, "
                    "falling back to default persona resolution", e
                )
                return resolve_persona_sync()
            if flagged:
                logger.warning(
                    "session.persona pref failed identity scan — ignoring pref, "
                    "falling back to default persona resolution"
                )
                return resolve_persona_sync()
            # THE shared selector order (F1): template key > character slug >
            # literal text. The scan above already ran on the RAW pref value, so
            # the literal branch is unchanged and the character branch only
            # reaches the same operator-authored file tier
            # PERSONALITY_DEFAULT_CHARACTER selects.
            from agents.personality.persona_resolver import resolve_persona_selector
            resolved = resolve_persona_selector(pref_persona)
            if resolved:
                return resolved
            return pref_persona
    except Exception:
        pass  # fail-open to the legacy resolver
    return resolve_persona_sync()


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

    from agents.task.constants import _FALSEY
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
            from agents.task.constants import task_personality_block_enabled
            gate = task_personality_block_enabled()
        except Exception:
            return info
    if not gate:
        return info
    info["gate"] = True

    selector, source = "", ""
    if user_id:
        try:
            import os
            from core.prefs import resolve_with_source
            env_persona = (os.environ.get("POLYROB_PERSONA") or "").strip() or None
            pref_persona, src = resolve_with_source(
                "session.persona", user_id, data_dir_or_home(home_dir),
                env_value=env_persona, default=None,
            )
            if src == "pref" and pref_persona:
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
        return f"persona: character '{name}' via {via} ({where})"
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
        "Selector order for /persona <value> and POLYROB_PERSONA: "
        "template key > character slug > literal text.",
        "Characters: set one persistently with "
        "PERSONALITY_DEFAULT_CHARACTER=<name> (or /persona <name>).",
        "Templates: set one with POLYROB_PERSONA=<template> "
        "(or /persona <template>).",
        "Either way the change applies to the NEXT session. "
        "`polyrob persona show` prints the rendered block.",
    ]
    if active["gate"]:
        where = f" ({active['path']})" if active.get("path") else ""
        guidance.insert(0, f"active: {active['kind']} {active['name']!r}"
                           f" via {active['source']}{where}")
    else:
        guidance.insert(0, "active: none — TASK_PERSONALITY_BLOCK is off, so no "
                           "persona block is injected")

    return {
        "templates": [[f"{k}{mark('template', k)}", "built-in operating template"]
                      for k in sorted(TEMPLATES)],
        "characters": [[f"{p}{mark('character', p)}", character_bio(p)[:80]]
                       for p in names],
        "guidance": guidance,
        "active": active,
    }
