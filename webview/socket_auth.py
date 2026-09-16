"""Bound the lifetime of authenticated streaming connections."""
import asyncio
import time
import math
import logging

from core.token_denylist import jti_is_revoked

logger = logging.getLogger(__name__)


class SocketAuthMonitor:
    """Disconnect expired/revoked sockets, including already joined rooms.

    Keep claims rather than raw bearer tokens. Each socket's watcher is
    cancelled on disconnect; revocations in other processes are observed too.
    """

    def __init__(self, disconnect, interval=5.0):
        self.disconnect = disconnect
        self.interval = interval
        self.tasks = {}

    def register(self, sid, claims):
        self.remove(sid)
        if claims.get("user_id"):
            self.tasks[sid] = asyncio.create_task(self._watch(sid, dict(claims)))

    def remove(self, sid):
        task = self.tasks.pop(sid, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def aclose(self):
        tasks = list(self.tasks.values())
        self.tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch(self, sid, claims):
        current = asyncio.current_task()
        invalid = False
        try:
            while True:
                await asyncio.sleep(self.interval)
                try:
                    expiry = float(claims.get("exp", 0))
                    invalid = (invalid or not math.isfinite(expiry) or expiry <= time.time()
                               or await asyncio.wait_for(
                                   asyncio.to_thread(jti_is_revoked, claims), timeout=2.0))
                except Exception:
                    logger.exception("socket authentication recheck failed")
                    invalid = True
                if invalid:
                    try:
                        await self.disconnect(sid)
                        return
                    except Exception:
                        logger.exception("socket disconnect failed; retrying")
        finally:
            if self.tasks.get(sid) is current:
                self.tasks.pop(sid, None)
