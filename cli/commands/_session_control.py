"""Send a durable control request; never confuse metadata with execution."""
import click

from core.session_control import CANCELLED_ALREADY, SessionControl


async def control_live_session(info, directory, user_id, session_id, command):
    """*info* / *directory*: the session's metadata and tree in the admin home
    (``cli.commands._session_store.read_session``); None when there is none."""
    if not info or directory is None or info.get("user_id") != user_id:
        raise click.ClickException(f"Session {session_id} not found for this user.")
    store = SessionControl(directory)
    state = await store.request_and_wait(command)
    if state is None:
        if command == "resume" and info.get("status") in {"suspended", "failed", "completed"}:
            click.echo(f'No live run. Continue with: polyrob run --continue {session_id} "your message"')
            return
        raise click.ClickException(
            "No live control endpoint for this session. Its stored status was not changed. "
            "An older running process must be stopped in its own terminal."
        )
    if state == CANCELLED_ALREADY:  # 098 CO-05: a stop is never erased
        raise click.ClickException(
            f"Session {session_id} is stopping: a cancel request holds it. Nothing was changed.")
    if state != "pending":
        click.echo(f"Session {session_id}: {state} (acknowledged at a step boundary).")
        return
    click.echo(f"Session {session_id}: {command} requested; awaiting a step boundary. "
               "An in-flight operation may still finish. Inspect with polyrob session show " + session_id)


async def queue_resume_message(task_agent, orchestrator, session_id, user_id, text, typed_text=None):
    """113 W0: `polyrob run --continue ID "message"` — the message becomes the chat's next
    line, on the same queue a chat surface uses, before the run starts. The terminal is
    an owner seat, so the line joins the owner thread (via ``repl``)."""
    typed = text if typed_text is None else typed_text
    await orchestrator.submit_user_message(
        agent_id=None, text=text, kind="comment", metadata={"typed_text": typed})
    try:
        from core.surfaces.owner_thread import record_owner_in
        record_owner_in(getattr(task_agent, "container", None), str(user_id or ""), typed,
                        via="repl", session_id=session_id)
    except Exception:
        pass
