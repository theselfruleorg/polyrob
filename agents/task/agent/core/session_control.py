"""Agent-side lifecycle and step-boundary handling of durable owner controls."""
import asyncio
import time
from functools import wraps

from core.session_control import SessionControl


def controlled_run(run):
    @wraps(run)
    async def wrapped(self, *args, **kwargs):
        if getattr(self, "_is_sub_agent", False) or not isinstance(getattr(self, "session_id", None), str):
            return await run(self, *args, **kwargs)
        from agents.task.path import pm
        store = SessionControl(pm().get_session_root(self.session_id, self.user_id))
        generation = store.start()
        self._session_control = (store, generation)
        try:
            return await run(self, *args, **kwargs)
        finally:
            store.finish(generation)
            self._session_control = None
    return wrapped


async def control_checkpoint(agent):
    control = getattr(agent, "_session_control", None)
    if not control:
        return True
    store, generation = control
    while True:
        row = store.read()
        if not row or row["generation"] != generation:
            raise RuntimeError("Session control generation changed during execution")
        command = row["request"]
        run_control = getattr(getattr(agent, "state", None), "run_control", None)
        if command != "pause" and run_control is not None and run_control.state == "paused":
            _resume(agent, run_control)  # 098 AG-03: pause time leaves the stall clock
        if command == "cancel":
            agent.cancel()
            orchestrator = getattr(agent, "orchestrator", None)
            if orchestrator is not None:
                orchestrator.cancel()
                if hasattr(orchestrator, "cancel_background_delegations"):
                    orchestrator.cancel_background_delegations("owner cancelled the run")
            store.acknowledge(generation, command, "cancelled")
            return False
        if command != "pause":
            if row["acknowledged"] != "running":
                store.acknowledge(generation, command, "running")
            return True
        if row["acknowledged"] != "paused":
            store.acknowledge(generation, command, "paused")
        if run_control is not None:
            run_control.pause()
        await asyncio.sleep(0.2)


def _resume(agent, run_control) -> None:
    """``paused -> running``; both stall clocks restart at the resume."""
    run_control.resume()
    now = time.time()
    agent.state.last_activity_time = now
    agent.state.last_step_start_time = now
