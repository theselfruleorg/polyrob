"""031: the pre-LLM owner stop/resume path for Telegram (kept out of the harness
god-file).

Runs AFTER voice transcription and the empty-content guard, BEFORE
``act_on_inbound``, only for the admin owner. A full stop or resume is applied
here and confirmed from the READ-BACK state — no model call, no HITL queue, no
tool path in the way (the three ways a stop went missing in the 2026-09-02
week). The text is still recorded into the bound session (``kind=
"owner_directive"``, framed as ALREADY APPLIED) so the agent's history has it —
but no model turn is spawned for it, and the agent cannot re-apply a stale
"stop" after a later ``/resume``.

A ``scoped`` intent ("stop trading for 6h") passes through to the agent, which
narrows it via the ``autonomy_control`` action — unless the model path is
known-dead (credit sentinel) or the session is busy, in which case the safe
default is a full pause.
"""
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

OWNER_DIRECTIVE_KIND = "owner_directive"


def _model_path_dead() -> bool:
    try:
        from core.credit_sentinel import credit_sentinel_active
        return bool(credit_sentinel_active())
    except Exception:
        logger.warning("owner intent gate: credit sentinel probe failed — assuming the model "
                       "is alive", exc_info=True)
        return False


def directive_note(raw_text: str, state_line: str) -> str:
    """The history note queued into the bound session: the owner's words, plus
    the fact that the surface already acted on them (so a later drain never
    re-applies a stale stop after a resume)."""
    stamp = time.strftime("%H:%M UTC", time.gmtime())
    return (f"[owner directive at {stamp}, ALREADY APPLIED by the surface — do not re-apply; "
            f"current state: {state_line}]\n{raw_text}")


async def handle_owner_intent(task_agent: Any, result: Any, intent: Any, data_dir: str,
                             *, busy: bool = False) -> Optional[str]:
    """The Telegram wrapper around ``owner_admin.apply_owner_intent`` (the ONE
    decision path): it supplies this seat's verbs, the busy / model-dead
    safe-default reason, and the owner-directive record into the bound session.
    Returns the reply text, or ``None`` when the message goes to the agent."""
    from core.surfaces.owner_admin import apply_owner_intent
    if intent is None:
        return None
    text = str(getattr(result.inbound, "text", "") or "")
    via = "telegram:voice" if text.startswith("[voice message") else "telegram"
    force = None
    if intent.kind == "scoped":
        if busy:
            force = "Your message was queued as busy"
        elif _model_path_dead():
            force = "My model is unavailable"
    reply, res = apply_owner_intent(intent, data_dir, via=via, resume_hint="/resume", halt_hint="/pause",
                        force_full_reason=force)
    if reply is None or res is None:
        return None
    sid = getattr(result.decision, "session_id", None)
    if sid:
        st = res.state
        state_line = (f"paused ({', '.join(st.scopes)})" if st.paused else "running")
        try:
            await task_agent.ensure_session_and_deliver(
                result.inbound.identity.user_id, sid, directive_note(text, state_line),
                kind=OWNER_DIRECTIVE_KIND)
        except Exception:
            logger.warning("owner intent gate: could not record the directive into %s", sid,
                           exc_info=True)
    return reply
