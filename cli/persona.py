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
            from agents.task.templates import TEMPLATES, resolve_template_persona
            if pref_persona in TEMPLATES:
                return resolve_template_persona(pref_persona)
            return pref_persona
    except Exception:
        pass  # fail-open to the legacy resolver
    return resolve_persona_sync()
