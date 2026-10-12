"""The drain + restart half of 077, shared by ``polyrob gateway`` and every
single-surface command (``_surface_runner``) — the processes that run chat
surfaces and the autonomy loops.

``core.lifecycle`` holds the records; this module ACTS on them:

* :class:`LifecycleWatcher` polls ``<data_home>/lifecycle/request.json`` and,
  when it claims a request, drains:

  1. **Hold autonomy** — ``core.lifecycle.begin_drain()``: the cron tick and the
     goal dispatcher start nothing new. Intake stays ON, so the owner is never
     cut off while the drain waits.
  2. **Wait for idle** — no live human turn, no running goal, no running cron
     job (``cli.update.agent_state.observe`` without the "due soon" signal: a
     due job cannot start while draining and would keep the drain busy for
     ever).
  3. **Stop intake** at the first idle moment — the surfaces stop polling
     (Telegram keeps unacknowledged updates server-side; the dedup store drops
     a replay) — and wait for the last batch to finish.
  4. **Exit 75** — the supervisor starts the process again.

  A drain that does not reach idle within the request's timeout is NOT forced:
  autonomy resumes, the request closes as ``drain_timeout`` and the requester
  is told what was busy. ``force`` skips the wait and says so.

* :func:`stop_drain` — SIGTERM (``systemctl stop``, an OS shutdown) also
  finishes the live turn instead of killing it, bounded by
  ``STOP_DRAIN_SEC``; a second signal stops at once.

* :func:`announce_boot` — after the surfaces are up: stamp ``healthy_at`` and
  send ONE "back online on <version>" (or the update's outcome) to the chat
  that asked, closing the request.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from core import lifecycle as lc

logger = logging.getLogger(__name__)

#: How often the watcher looks for a request.
POLL_SEC = 2.0
#: How long a SIGTERM stop waits for the live work (systemd's default
#: TimeoutStopSec is 90 s; the units this tree writes give 120 s).
STOP_DRAIN_SEC = 60.0
#: After intake stops, how long the last polled batch may still run.
INTAKE_GRACE_SEC = 120.0

#: Surfaces whose ``chat_id`` is a deliverable address. The console's chat id
#: is a session id and the REPL/CLI have none — those get the owner's default
#: route (or, for the CLI, the command that waits prints the result itself).
_NO_REPLY_SURFACES = frozenset({"cli"})
_NOT_AN_ADDRESS = frozenset({"webview", "console", "repl"})


def busy_description(data_home: Path) -> str:
    """What the drain is waiting on, in plain words ("idle" when nothing)."""
    from cli.update.agent_state import observe
    act = observe(data_home, cron_imminent_sec=None)
    return act.describe()


def _is_idle(data_home: Path) -> tuple[bool, str]:
    from cli.update.agent_state import observe
    act = observe(data_home, cron_imminent_sec=None)
    return act.idle, act.describe()


async def notify(container: Any, req: Optional[lc.LifecycleRequest], text: str) -> str:
    """Send one lifecycle message to the chat that asked. Never raises."""
    if req is None or container is None:
        return "no_request"
    origin = dict(req.origin or {})
    surface = str(origin.get("surface") or "")
    user_id = str(origin.get("user_id") or "")
    if surface in _NO_REPLY_SURFACES or not user_id:
        logger.info("lifecycle: %s", text)
        return "no_reply_surface"
    try:
        from core.surfaces.user_delivery import deliver_user_message
        kwargs = {}
        chat_id = str(origin.get("chat_id") or "")
        if chat_id and surface and surface not in _NOT_AN_ADDRESS:
            kwargs = {"recipient_override": chat_id, "recipient_surface": surface}
        return await deliver_user_message(container, user_id, text, source="service",
                                          priority="critical", **kwargs)
    except Exception:
        logger.warning("lifecycle: could not deliver %r", text, exc_info=True)
        return "error"


class LifecycleWatcher:
    """Watch for a restart request and drain this process for it.

    ``stop_intake`` makes the surfaces stop taking messages (sync, idempotent);
    ``intake_done`` says whether the surfaces' run loops have returned;
    ``request_exit`` ends the process's main wait (it then runs its normal
    shutdown and exits with :data:`core.lifecycle.RESTART_EXIT_CODE`).
    """

    def __init__(self, *, data_home: Path, container: Any,
                 stop_intake: Callable[[], None],
                 intake_done: Callable[[], bool],
                 request_exit: Callable[[], None],
                 serves: frozenset = frozenset(), primary: bool = True,
                 poll: float = POLL_SEC,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        self.data_home = Path(data_home)
        self.container = container
        self.stop_intake = stop_intake
        self.intake_done = intake_done
        self.request_exit = request_exit
        self.serves = frozenset(serves)
        self.primary = primary
        self.poll = poll
        self._clock = clock
        self._sleep = sleep
        self.exit_code: Optional[int] = None
        self.request: Optional[lc.LifecycleRequest] = None
        #: Set by a SIGTERM/SIGINT stop: a drain in progress stops waiting.
        self.aborted = False

    def abort(self) -> None:
        """The process is stopping for another reason (``systemctl stop``, a
        deploy, Ctrl-C). A drain in progress gives up waiting and leaves its
        request ``exiting`` — the next start of this service answers it (and
        applies an asked-for update) — so the stop is never held up by it."""
        self.aborted = True

    async def run(self) -> None:
        """Poll until a request is claimed and drained (or the task is cancelled)."""
        while self.exit_code is None and not self.aborted:
            try:
                req = lc.claim_request(data_home=self.data_home, serves=self.serves,
                                       primary=self.primary)
            except Exception:
                logger.warning("lifecycle: request check failed", exc_info=True)
                req = None
            if req is not None:
                try:
                    await self.drain(req)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A drain that dies must not leave the request "draining" for
                    # ever (the live claimer would block every later /restart).
                    logger.error("lifecycle: drain failed for %s", req.describe(), exc_info=True)
                    lc.end_drain()
                    try:
                        lc.close_request(lc.OUTCOME_CANCELLED, "the drain failed (see the log)",
                                         data_home=self.data_home)
                    except Exception:
                        logger.warning("lifecycle: could not close the request", exc_info=True)
                    self.request = None
                    await notify(self.container, req,
                                 "I could not restart: the drain failed. Nothing changed; "
                                 "the details are in the service log.")
                if self.exit_code is not None or self.aborted:
                    return
            await self._sleep(self.poll)

    async def drain(self, req: lc.LifecycleRequest) -> None:
        self.request = req
        sup = lc.supervisor()
        if not sup:
            lc.close_request(lc.OUTCOME_CANCELLED,
                             "nothing would start this process again (no supervisor)",
                             data_home=self.data_home)
            await notify(self.container, req,
                         "I can't restart here: nothing would start me again. "
                         "Run me as a background service (`polyrob service install`).")
            self.request = None
            return
        lc.begin_drain()
        idle, busy = _is_idle(self.data_home)
        what = "update" if req.kind == lc.KIND_UPDATE else "restart"
        if req.force:
            await notify(self.container, req,
                         f"Restarting now for the {what}"
                         + ("" if idle else f" — this cuts: {busy}") + ".")
        elif not idle:
            await notify(self.container, req,
                         f"I {what} once the current work finishes ({busy}). "
                         f"I wait up to {int(req.drain_timeout // 60) or 1} min.")
        deadline = self._clock() + max(0.0, float(req.drain_timeout))
        while not req.force and not idle:
            if self.aborted:
                self._leave_for_next_start(req)
                return
            if self._clock() >= deadline:
                lc.end_drain()
                lc.close_request(lc.OUTCOME_DRAIN_TIMEOUT, busy, data_home=self.data_home)
                await notify(self.container, req,
                             f"I did not {what}: still busy after "
                             f"{int(req.drain_timeout)} s ({busy}). Nothing changed. "
                             f"Ask again later, or use `/{what} now` to cut the work.")
                self.request = None
                return
            await self._sleep(self.poll)
            idle, busy = _is_idle(self.data_home)
        # Idle (or forced): stop intake and let the last batch finish.
        try:
            self.stop_intake()
        except Exception:
            logger.warning("lifecycle: stop_intake failed", exc_info=True)
        grace = self._clock() + (0.0 if req.force else INTAKE_GRACE_SEC)
        while not self.intake_done() and self._clock() < grace and not self.aborted:
            await self._sleep(min(self.poll, 1.0))
        if not req.force:
            # A message from the last batch may have started a turn.
            while (self._clock() < grace and not self.aborted
                   and not _is_idle(self.data_home)[0]):
                await self._sleep(min(self.poll, 1.0))
        if self.aborted:
            self._leave_for_next_start(req)
            return
        lc.set_request_state(lc.STATE_EXITING, data_home=self.data_home,
                             exited_at=time.time())
        logger.warning("lifecycle: exiting with %s for %s", lc.RESTART_EXIT_CODE, req.describe())
        self.exit_code = lc.RESTART_EXIT_CODE
        self.request_exit()

    def _leave_for_next_start(self, req: lc.LifecycleRequest) -> None:
        lc.set_request_state(lc.STATE_EXITING, data_home=self.data_home,
                             exited_at=time.time(), note="stopped during the drain")
        logger.warning("lifecycle: stopping during the drain for %s — the next start "
                       "answers it", req.describe())


async def stop_drain(*, data_home: Path, stop_intake: Callable[[], None],
                     intake_done: Callable[[], bool], timeout: float = STOP_DRAIN_SEC,
                     clock: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> bool:
    """SIGTERM: stop intake, wait (bounded) for the live work. True when the
    work finished inside the bound, False when the stop cuts it."""
    lc.begin_drain()
    try:
        stop_intake()
    except Exception:
        logger.warning("lifecycle: stop_intake failed", exc_info=True)
    deadline = clock() + max(0.0, timeout)
    while clock() < deadline:
        if intake_done() and _is_idle(data_home)[0]:
            return True
        await sleep(0.5)
    logger.warning("lifecycle: stop after %.0f s with work still running: %s",
                   timeout, busy_description(data_home))
    return False


def install_stop_signals(loop: asyncio.AbstractEventLoop, *, data_home: Path,
                         stop_intake: Callable[[], None], intake_done: Callable[[], bool],
                         stop_now: Callable[[], None],
                         abort: Callable[[], None] = lambda: None) -> None:
    """SIGTERM drains (bounded by :data:`STOP_DRAIN_SEC`) then stops; SIGINT and a
    second SIGTERM stop at once. ``stop_now`` is the process's old immediate stop."""
    import signal as _signal
    state = {"draining": False}

    async def _graceful():
        try:
            await stop_drain(data_home=data_home, stop_intake=stop_intake,
                             intake_done=intake_done)
        finally:
            stop_now()

    def _on_term(*_a):
        abort()  # a restart drain in progress yields to the stop at once
        if state["draining"]:
            stop_now()
            return
        state["draining"] = True
        logger.warning("SIGTERM: finishing the live work (up to %.0f s) before stopping",
                       STOP_DRAIN_SEC)
        asyncio.ensure_future(_graceful())

    def _on_int(*_a):
        abort()
        stop_now()

    try:
        loop.add_signal_handler(_signal.SIGINT, _on_int)
        loop.add_signal_handler(_signal.SIGTERM, _on_term)
    except (NotImplementedError, RuntimeError):
        for _sig, fn in ((_signal.SIGINT, _on_int), (_signal.SIGTERM, _on_term)):
            try:
                _signal.signal(_sig, lambda *_a, _fn=fn: loop.call_soon_threadsafe(_fn))
            except (ValueError, OSError, AttributeError):
                pass  # SIGTERM may be unavailable on some platforms/threads


def _boot_message(req: lc.LifecycleRequest, boot: dict, now: float) -> tuple[str, str]:
    """(outcome, owner text) for the request a healthy boot answers."""
    took = max(0, int(now - float(req.created_at or now)))
    label = lc.version_label(boot.get("version"), boot.get("sha"))
    res = dict(req.update_result or {})
    if req.kind == lc.KIND_UPDATE:
        if res.get("ok"):
            return (lc.OUTCOME_UPDATED,
                    f"✅ Updated and back online on {label} "
                    f"(was {lc.version_label(req.from_version, req.from_sha)}) — took {took} s.")
        if res.get("boot_failed"):
            return (lc.OUTCOME_BOOT_FAILED,
                    f"⚠️ The update to {res.get('to_version') or 'the new version'} did not "
                    f"start, so I rolled it back. Back online on {label}.")
        if res.get("no_update"):
            return (lc.OUTCOME_BACK_ONLINE,
                    f"✅ Already on the latest version — restarted. Back online on {label}.")
        if res:
            return (lc.OUTCOME_UPDATE_FAILED,
                    f"⚠️ The update failed at the '{res.get('failed_step') or 'apply'}' step: "
                    f"{str(res.get('error') or 'unknown error')[:300]}. "
                    + ("It was rolled back. " if res.get("rolled_back") else
                       "The rollback was incomplete — run `polyrob update --list-snapshots`. ")
                    + f"Back online on {label}.")
        return (lc.OUTCOME_UPDATE_FAILED,
                f"⚠️ I restarted but no update ran. Back online on {label}.")
    return lc.OUTCOME_BACK_ONLINE, f"✅ Back online on {label} — restart took {took} s."


async def announce_boot(container: Any, data_home: Path, role: str = "",
                        serves: frozenset = frozenset()) -> Optional[str]:
    """Stamp the healthy start and answer the request it closes. Never raises.

    A secondary process (``role``) answers only the restart it could have
    claimed — one from a surface it serves — never the primary's."""
    try:
        boot = lc.mark_healthy(data_home=data_home, role=role) or {}
        req = lc.read_request(data_home=data_home)
    except Exception:
        logger.warning("lifecycle: boot record unreadable", exc_info=True)
        return None
    if req is None or req.state != lc.STATE_EXITING:
        return None
    if role and (req.kind == lc.KIND_UPDATE
                 or str((req.origin or {}).get("surface") or "") not in serves):
        return None
    outcome, text = _boot_message(req, boot, time.time())
    lc.close_request(outcome, text, data_home=data_home,
                     version=boot.get("version"), sha=boot.get("sha"))
    await notify(container, req, text)
    return text


__all__ = ["LifecycleWatcher", "stop_drain", "announce_boot", "notify",
           "install_stop_signals",
           "busy_description", "POLL_SEC", "STOP_DRAIN_SEC"]
