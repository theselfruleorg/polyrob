"""Persona resolution for CLI surfaces (REPL + one-shot run).

``resolve_cli_persona()`` is the single helper used by both ``polyrob run``
(one-shot) and the interactive REPL so the init-chosen persona is wired
consistently into every CLI agent path.
"""
import os


def resolve_cli_persona() -> str:
    """Persona text for the CLI agent's <identity>, or "" when the gate is off.

    Reads the init-chosen template (POLYROB_PERSONA) and renders its persona
    text only when task_personality_block_enabled() is true (default ON local).
    """
    from agents.task.constants import task_personality_block_enabled
    if not task_personality_block_enabled():
        return ""
    from agents.task.templates import TEMPLATES, resolve_template_persona
    val = (os.environ.get("POLYROB_PERSONA") or "").strip()
    if val and val not in TEMPLATES:
        # A non-template value is used as LITERAL persona text (a free-form persona),
        # rather than silently degrading to the "general" template. Known template
        # keys still render their persona; empty/unset falls to the default.
        return val
    return resolve_template_persona(val or None)
