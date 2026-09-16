"""h_steer.py — ``/steer`` in the REPL (043 A23).

New on every seat. ``/steer <guidance>`` injects a guidance message into the
BOUND session without ending the turn — the agent reads it as user guidance at
its next step. It is the SAME rail the console's Steer button and the HTTP
``POST /sessions/{id}/messages`` path use: ``orchestrator.submit_user_message``
(HITL ingress, ``kind="comment"``) → ``inject_user_guidance``. It adds no new
authority — steering IS the existing message endpoint (043 global constraint).

Async on purpose: ``submit_user_message`` is a coroutine and the registry
awaits an awaitable handler (``CommandRegistry.dispatch``).
"""
from __future__ import annotations


async def h_steer(ctx) -> None:
    """Nudge the running session with one guidance message; report honestly."""
    text = " ".join(ctx.args or []).strip()
    if not text:
        ctx.emit(
            "Usage: /steer <guidance> — nudge the running session without\n"
            "  ending the turn. It reaches the agent at its next step.",
            title="steer",
        )
        return
    orch = getattr(ctx, "orchestrator", None)
    if orch is None or not hasattr(orch, "submit_user_message"):
        ctx.emit(
            "No live session is bound here — /steer needs a resident session\n"
            "  to guide. Just type to talk to the agent directly.",
            title="steer",
        )
        return
    try:
        # agent_id=None routes to the session's first agent; kind="comment" is
        # the trusted-human intake kind (context-ref expansion allowed, forged
        # kinds are not).
        await orch.submit_user_message(None, text, kind="comment")
    except Exception as exc:  # queue-full or a torn-down session: say so
        ctx.emit(f"  Could not steer the session ({exc}).", title="steer")
        return
    ctx.emit("Steered — the agent will see this at its next step.", title="steer")


HELP_STEER = (
    "  Send one guidance message into the running session WITHOUT ending the\n"
    "  turn. The agent reads it at its next step and keeps going — use it to\n"
    "  redirect, add a constraint, or answer a question it raised mid-run.\n"
    "\n"
    "  This is the same rail the console's Steer button uses. It adds no new\n"
    "  authority: it is the ordinary message endpoint, not an override of any\n"
    "  gate.\n"
    "\n"
    "    /steer prefer the cheaper provider for the rest of this run",
    "The Steer button on the chat receipt in the console.",
)


def register(reg, Command) -> None:
    """Register ``/steer``. Called from ``h_a23.register``."""
    reg.register(Command(
        "steer", h_steer,
        "Nudge the running session with one guidance message (turn keeps going)",
        usage="<guidance>", group="talk", raw_arguments=True,
        help_long=HELP_STEER[0], elsewhere=HELP_STEER[1],
    ))
