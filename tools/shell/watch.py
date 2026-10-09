"""Background-job watcher (073 W4): durable receipt on exit + optional notify.

ONE asyncio task per background job, on the loop that launched it (no thread, no
new service). It polls the job's executor:

- when the job is no longer running, it writes the durable receipt
  (``tools/shell/receipts.py``) and, with ``notify``, delivers ONE exit note;
- with ``notify="pattern"`` it reads the NEW log bytes each poll, matches each
  complete line against the regex, and delivers a note with the matching lines —
  at most one note per ``PATTERN_MIN_INTERVAL_SEC`` (matches in between are
  batched), and the pattern notify turns itself off after ``PATTERN_MAX_NOTES``
  notes. A pattern watch still sends the exit note.

Delivery is the caller's ``deliver(text, kind)`` coroutine — the shell tool binds
it to ``TaskAgent.deliver_self_wake``, the existing self-wake rail, so a note
arrives as a FORGED turn (``turn_kind=self_wake``): the woken turn reads the note
as data and gets no compute posture (``compute_posture_allows`` refuses a forged
turn), the re-entry budget and the owner pause apply, and the text is wrapped as
untrusted data. The watcher never decides anything about posture itself.

No ``@BaseTool.action`` closures — ``from __future__`` is safe.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

NOTIFY_MODES = ("exit", "pattern")
PATTERN_MAX_NOTES = 5
PATTERN_MIN_INTERVAL_SEC = 10.0
_MAX_LINES_PER_NOTE = 5
_MAX_LINE_CHARS = 300
_EXIT_TAIL_CHARS = 2000
_MAX_CONSECUTIVE_ERRORS = 5
#: poll interval (seconds): a host poll is a syscall; a container poll is a
#: ``docker exec``, so it is slower — and slower again with no notify to serve.
_INTERVALS = {"host": 1.0, "docker": 3.0}
_IDLE_DOCKER_INTERVAL = 15.0

Deliver = Callable[[str, str], Awaitable[bool]]

#: (session_id, job_id) -> task; holds a strong ref (a bare create_task is weak).
_WATCHES: Dict[Tuple[str, str], "asyncio.Task"] = {}


class JobWatch:
    def __init__(self, *, executor, job, session_id: str, user_id: Optional[str],
                 registry=None, notify: Optional[str] = None,
                 pattern: Optional["re.Pattern"] = None,
                 deliver: Optional[Deliver] = None,
                 interval: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.executor = executor
        self.job = job
        self.session_id = session_id
        self.user_id = user_id
        self.registry = registry
        self.notify = notify if deliver is not None else None
        self.pattern = pattern if self.notify == "pattern" else None
        self.deliver = deliver
        kind = getattr(executor, "kind", "docker")
        if interval is None:
            interval = _INTERVALS.get(kind, 3.0)
            if kind == "docker" and self.notify is None:
                interval = _IDLE_DOCKER_INTERVAL
        self.interval = interval
        self._clock = clock
        self._offset = 0
        self._partial = ""
        self._pending: list = []
        self._notes_sent = 0
        self._last_note_at: Optional[float] = None
        self.delivered: list = []   # (kind, ok) for tests / audit

    # --- pattern -----------------------------------------------------------------
    async def _scan(self) -> None:
        if self.pattern is None:
            return
        if not hasattr(self.executor, "read_log_from"):
            self.pattern = None  # this backend cannot stream its log
            return
        text, self._offset = await self.executor.read_log_from(self.job.id, self._offset)
        if not text:
            return
        from tools.shell.output import strip_ansi
        buf = self._partial + strip_ansi(text).replace("\r\n", "\n").replace("\r", "\n")
        lines = buf.split("\n")
        self._partial = lines.pop()[-4096:]
        for line in lines:
            if self.pattern.search(line[:4096]):
                self._pending.append(line[:_MAX_LINE_CHARS])

    async def _flush_pattern(self, *, final: bool = False) -> None:
        if self.pattern is None:
            return
        if final and self._partial and self.pattern.search(self._partial):
            self._pending.append(self._partial[:_MAX_LINE_CHARS])
            self._partial = ""
        if not self._pending:
            return
        now = self._clock()
        if (not final and self._last_note_at is not None
                and now - self._last_note_at < PATTERN_MIN_INTERVAL_SEC):
            return
        lines = self._pending[:_MAX_LINES_PER_NOTE]
        more = len(self._pending) - len(lines)
        self._pending = []
        self._notes_sent += 1
        self._last_note_at = now
        off = self._notes_sent >= PATTERN_MAX_NOTES
        from core.secret_scrub import scrub_secret_shapes
        body = "\n".join(scrub_secret_shapes(line) for line in lines)
        text = (f"Background job `{self.job.id}` printed output that matches your notify "
                f"pattern /{self.pattern.pattern}/ (command: {self.job.command[:200]}):\n"
                f"{body}"
                + (f"\n(+{more} more matching lines)" if more > 0 else "")
                + (f"\nPattern notify is now OFF for this job ({PATTERN_MAX_NOTES} notes "
                   f"sent); use `process` poll/log to follow it." if off else ""))
        await self._send(text, "shell_job_match")
        if off:
            self.pattern = None

    # --- exit --------------------------------------------------------------------
    async def _on_exit(self) -> None:
        from tools.shell.receipts import finalize_job
        await finalize_job(self.executor, self.job, session_id=self.session_id,
                           user_id=self.user_id, registry=self.registry)
        if self.notify is None or getattr(self.job, "status", "") == "killed":
            return  # the agent killed it itself: nothing to tell it
        try:
            await self._scan()
        except Exception:
            pass
        await self._flush_pattern(final=True)
        rc = None
        tail = ""
        try:
            rc = await self.executor.exit_code(self.job.id)
            tail = await self.executor.read_log(self.job.id, max_bytes=_EXIT_TAIL_CHARS * 2)
        except Exception:
            pass
        from tools.shell.output import exit_note, shape_output
        tail_text, _ = shape_output(tail or "", max_chars=_EXIT_TAIL_CHARS)
        if rc is None:
            head = f"Background job `{self.job.id}` finished (exit code unknown)"
        else:
            note = exit_note(rc)
            head = f"Background job `{self.job.id}` finished: exit {rc}" + (f" — {note}" if note else "")
        text = (f"{head}. Command: {self.job.command[:200]}\n--- log tail ---\n"
                f"{tail_text or '(no output)'}")
        await self._send(text, "shell_job_exit")

    async def _send(self, text: str, kind: str) -> None:
        ok = False
        try:
            ok = bool(await self.deliver(text, kind))
        except Exception:
            logger.debug("shell job notify delivery raised (fail-open)", exc_info=True)
        self.delivered.append((kind, ok))
        if not ok:
            logger.info("shell job %s: %s note not delivered (self-wake off, budget "
                        "spent, paused, or session gone)", self.job.id, kind)

    # --- loop --------------------------------------------------------------------
    async def run(self) -> None:
        errors = 0
        while True:
            try:
                await self._scan()
                await self._flush_pattern()
                status = await self.executor.poll(self.job.id)
                errors = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                errors += 1
                logger.debug("shell job watch poll failed", exc_info=True)
                if errors >= _MAX_CONSECUTIVE_ERRORS:
                    return  # the executor is gone (session torn down)
                await asyncio.sleep(self.interval)
                continue
            if status != "running":
                try:
                    await self._on_exit()
                except Exception:
                    logger.debug("shell job watch exit handling failed", exc_info=True)
                return
            await asyncio.sleep(self.interval)


def start_watch(**kwargs) -> Optional["asyncio.Task"]:
    """Start a :class:`JobWatch` on the running loop; None when no loop runs."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    w = JobWatch(**kwargs)
    key = (w.session_id, w.job.id)
    task = loop.create_task(w.run(), name=f"shell-watch-{w.job.id}")
    task.watch = w  # type: ignore[attr-defined]
    _WATCHES[key] = task
    task.add_done_callback(lambda _t, _k=key: _WATCHES.pop(_k, None))
    return task


def get_watch(session_id: str, job_id: str) -> Optional["asyncio.Task"]:
    return _WATCHES.get((session_id, job_id))


def stop_watches(session_id: Optional[str] = None) -> list:
    """Cancel the watchers of one session (or all); returns the cancelled tasks.
    A session teardown may call it; the job's receipt is then written by the next
    ``process`` call that sees it finished."""
    out = []
    for key, task in list(_WATCHES.items()):
        if session_id is None or key[0] == session_id:
            task.cancel()
            out.append(task)
            _WATCHES.pop(key, None)
    return out


_QUANT_AFTER = re.compile(r"[*+]|\{\d*,\d*\}|\{\d{2,}\}")
_BACKREF = re.compile(r"\\[1-9]|\(\?P=")


def _has_nested_quantifier(raw: str) -> bool:
    """True when a repeated group holds a quantifier anywhere inside it
    (``(a+)+``, ``((\\w*))*``, ``(x{1,9}){2,}``) — the catastrophic-backtracking shape."""
    stack = [False]  # per open group: "a quantifier occurs inside"
    i, n, in_class = 0, len(raw), False
    while i < n:
        c = raw[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
            i += 1
            continue
        if c == "[":
            in_class = True
        elif c == "(":
            stack.append(False)
        elif c == ")" and len(stack) > 1:
            inner = stack.pop()
            m = _QUANT_AFTER.match(raw, i + 1)
            if m and inner:
                return True
            stack[-1] = stack[-1] or inner or bool(m)
            if m:
                i = m.end()
                continue
        elif c in "*+" or (c == "{" and _QUANT_AFTER.match(raw, i)) or c == "?":
            if c != "?" or (i > 0 and raw[i - 1] != "("):
                stack[-1] = stack[-1] or c != "?"
        i += 1
    return False


def compile_pattern(raw: str) -> "re.Pattern":
    """Compile a notify regex (bounded length); raises ``ValueError`` when bad."""
    if not raw or not raw.strip():
        raise ValueError("notify_pattern is empty")
    if len(raw) > 500:
        raise ValueError("notify_pattern is longer than 500 characters")
    # The pattern runs on the session's own event loop against every output line,
    # and ``re`` has no time bound. Refuse the shapes that backtrack
    # catastrophically: a quantified group that itself holds a quantifier
    # ("(a+)+", "(\\w*)*", "(x{1,9}){2,}") and backreferences.
    if _has_nested_quantifier(raw):
        raise ValueError("notify_pattern has a quantifier inside a repeated group "
                         "(catastrophic backtracking); use a simpler pattern")
    if _BACKREF.search(raw):
        raise ValueError("notify_pattern uses a backreference; use a simpler pattern")
    try:
        return re.compile(raw)
    except re.error as e:
        raise ValueError(f"notify_pattern is not a valid regex: {e}") from e


__all__ = ["JobWatch", "start_watch", "get_watch", "stop_watches", "compile_pattern", "NOTIFY_MODES",
           "PATTERN_MAX_NOTES", "PATTERN_MIN_INTERVAL_SEC"]
