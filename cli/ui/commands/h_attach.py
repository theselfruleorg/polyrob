"""Explicit file/image attachment ingress for terminal users."""


async def h_attach(ctx):
    if not ctx.args:
        ctx.emit('Usage: /attach "path to file" [more files]')
        return
    if ctx.orchestrator is None:
        ctx.emit("No live session is available for attachments.")
        return
    from cli.attachments import attach_to_session
    paths = await attach_to_session(ctx.orchestrator, ctx.session_id, ctx.user_id, ctx.args)
    ctx.emit("Attachments queued for the agent's next step:\n" + "\n".join(paths)
             + "\nType a message to continue an idle conversation.")


def register(reg, Command):
    reg.register(Command("attach", h_attach, "Attach local files or images to the next agent step",
                         usage="<path> [path…]", group="talk"))
