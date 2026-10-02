"""Send a durable control request; never confuse metadata with execution."""
import click

from core.session_control import SessionControl


async def control_live_session(task_agent, user_id, session_id, command):
    info = task_agent.session_manager.get_session_info(session_id)
    if not info or info.get("user_id") != user_id:
        raise click.ClickException(f"Session {session_id} not found for this user.")
    from agents.task.path import pm
    store = SessionControl(pm().get_session_root(session_id, user_id))
    state = await store.request_and_wait(command)
    if state is None:
        if command == "resume" and info.get("status") in {"suspended", "failed", "completed"}:
            click.echo(f"No live run. Continue with: polyrob run --resume {session_id}")
            return
        raise click.ClickException(
            "No live control endpoint for this session. Its stored status was not changed. "
            "An older running process must be stopped in its own terminal."
        )
    if state != "pending":
        click.echo(f"Session {session_id}: {state} (acknowledged at a step boundary).")
        return
    click.echo(f"Session {session_id}: {command} requested; awaiting a step boundary. "
               "An in-flight operation may still finish. Inspect with polyrob session show " + session_id)
