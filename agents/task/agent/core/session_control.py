"""Agent-side lifecycle and step-boundary handling of durable owner controls."""
import asyncio
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
        if command == "cancel":
            agent.cancel()
            orchestrator = getattr(agent, "orchestrator", None)
            if orchestrator is not None:
                orchestrator.cancel()
            store.acknowledge(generation, command, "cancelled")
            return False
        if command != "pause":
            if row["acknowledged"] != "running":
                store.acknowledge(generation, command, "running")
            return True
        if row["acknowledged"] != "paused":
            store.acknowledge(generation, command, "paused")
        await asyncio.sleep(0.2)
