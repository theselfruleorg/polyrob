"""113: the five chat verbs in the terminal (``core.surfaces.chats`` is the logic)."""
from __future__ import annotations

from cli.ui import candy

HELP_NEW = (
    "  Open a separate chat beside the main one. The chat you leave stays open\n"
    "  and keeps its history.\n"
    "\n"
    "  One terminal run is one chat, so the terminal starts again in the new\n"
    "  chat (a few seconds). `polyrob --new` starts there at once. `/clear`\n"
    "  empties this run's context and keeps the chat.",
    "`/new` on Telegram, where your next message starts the new chat.",
)


def _emit(ctx, verb: str) -> None:
    from cli.ui import chat_seat
    try:
        out = chat_seat.run_verb(ctx, verb)
    except Exception as exc:
        out = {"text": f"I could not read the chats ({type(exc).__name__}). Nothing was changed.",
               "restart": False}
    text = out["text"] + (f"\n{chat_seat.RESTARTING}" if out["restart"] else "")
    ctx.emit("\n".join(f"{candy.GUTTER}{line}" if line else "" for line in text.splitlines()),
             title=verb.lstrip("/"))
    if out["restart"]:
        from cli.ui.commands.registry import ReplExit
        chat_seat.want_restart()     # run_repl starts again once this run has closed
        raise ReplExit()


def h_new(ctx) -> None:
    """Open a separate chat beside the main one."""
    _emit(ctx, "/new")


def h_chats(ctx) -> None:
    """Your chats: the main one and each separate one."""
    _emit(ctx, "/chats")


def h_chat(ctx) -> None:
    """Switch to another of your chats."""
    _emit(ctx, "/chat")


def h_rename(ctx) -> None:
    """Name the chat this run is in."""
    _emit(ctx, "/rename")


def h_close(ctx) -> None:
    """Close a separate chat."""
    _emit(ctx, "/close")


def register(reg, Command) -> None:
    reg.register(Command("new", h_new, "Open a separate chat beside the main one",
                         usage="[name]", group="talk",
                         help_long=HELP_NEW[0], elsewhere=HELP_NEW[1]))
    # `/sessions` is the old name of this list (run ids: `polyrob session list`).
    reg.register(Command("chats", h_chats, "Your chats: the main one and each separate one",
                         group="talk", aliases=("sessions",)))
    reg.register(Command("chat", h_chat, "Switch to another of your chats",
                         usage="<number or name>", group="talk"))
    reg.register(Command("rename", h_rename, "Name the chat you are in",
                         usage="<name>", group="talk"))
    reg.register(Command("close", h_close, "Close a separate chat and go back to the main one",
                         usage="[number or name]", group="talk"))
