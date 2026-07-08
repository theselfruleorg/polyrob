"""Persona resolution for CLI surfaces (REPL + one-shot run).

``resolve_cli_persona()`` is the single helper used by both ``polyrob run``
(one-shot) and the interactive REPL so the init-chosen persona is wired
consistently into every CLI agent path.
"""


def resolve_cli_persona() -> str:
    """Persona text for the CLI agent's <identity>, or "" when the gate is off.

    T1-07: delegates to the surface-shared ``agents.personality.persona_resolver``
    so CLI and chat render the SAME voice: explicit POLYROB_PERSONA (template key
    or literal free-form text) > the default character > "".
    """
    from agents.personality.persona_resolver import resolve_persona_sync
    return resolve_persona_sync()
