"""Owner preference replies for the Telegram admin verbs: /model here; /prefs and
/config live in ``core/prefs_reply.py`` (proposal 125 S14) — import them there.

Moved out of ``surfaces/telegram/harness.py`` (W9, 2026-10-10 structural
review); the harness re-exports every name. Imports nothing from the harness.
"""
import logging

logger = logging.getLogger("surfaces.telegram.harness")


async def model_reply(task_agent, user_id: str, args: list, *, chat_key=None,
                      session_id=None, seat: str = "telegram") -> str:
    """`/model` (080 D8, 094): the LIVE chat's model and where it comes from;
    `/model <name>` switches THIS chat from the next step (no restart, no env
    write); `default`, `reasoning=`, `reset`, `aliases`, `alias` — the grammar is
    ``core/model_switch.py``, the pipeline ``agents/task/model_switch_service.py``
    (the REPL and the console run the same one)."""
    from agents.task.model_switch_service import model_command, picker_buttons
    try:
        text = await model_command(task_agent, user_id=user_id, words=args,
                                   chat_key=chat_key, session_id=session_id, seat=seat)
        if args:
            return text
        from core.surfaces.command_reply import CommandReply
        from core.surfaces.nav import actions
        return CommandReply(text=text, actions=tuple(actions(user_id, picker_buttons(
            task_agent, user_id=user_id, chat_key=chat_key, session_id=session_id))))
    except Exception as exc:  # noqa: BLE001 — a verb reply, never a crash
        logger.warning("/model failed", exc_info=True)
        return f"❌ /model failed ({type(exc).__name__}: {str(exc)[:160]}). Nothing changed."
