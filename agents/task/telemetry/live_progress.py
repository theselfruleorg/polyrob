"""Live per-turn progress from feed events (019 P2) — surface-agnostic core.

Turns the run-state feed events (tool_started / llm_started / step /
awaiting_approval / retry_wait / compaction_*) into throttled progress-bubble
text for a chat surface's ``ProgressReporter`` (e.g. Telegram's
``EditingProgressReporter``): ``⚙️ step 3 · → navigate · 2 tools · 45s · $0.02``,
with wait states overriding immediately (``⏸ Waiting for your approval…``).

Wiring: the surface registers ONE ``TurnProgressTracker`` per in-flight turn
via :func:`attach_tracker`; a process-wide feed subscriber (installed once via
``ProductTelemetry.add_feed_subscriber``) dispatches events by session id. A
tracker attached before the session id is known (a fresh chat turn) binds
lazily through a ``session_key`` + reverse-resolver match.

Everything here is best-effort display logic: never raises into the feed
writer, and a finished reporter swallows trailing edits (its ``_finished``
guard).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Feed kinds that override the throttle (the wait states — show immediately).
_WAIT_KINDS = ("awaiting_approval", "retry_wait", "compaction_started")

DEFAULT_EDIT_INTERVAL_SEC = 2.5
APPROVAL_REMINDER_SEC = 600.0


class TurnProgressTracker:
    """Feed-event → throttled progress text for ONE surface turn."""

    def __init__(
        self,
        reporter: Any,
        *,
        session_key: str = "",
        session_id: Optional[str] = None,
        key_resolver: Optional[Callable[[str], Optional[str]]] = None,
        min_edit_interval: float = DEFAULT_EDIT_INTERVAL_SEC,
        approval_reminder_sec: float = APPROVAL_REMINDER_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._reporter = reporter
        self.session_key = session_key
        self.session_id = session_id
        #: session_id → session_key reverse lookup for lazy binding.
        self._key_resolver = key_resolver
        self._no_match: set[str] = set()
        self._interval = min_edit_interval
        self._reminder_sec = approval_reminder_sec
        self._clock = clock
        self._started_at = clock()
        self._last_edit_at = float("-inf")
        self._last_text = ""
        self._step = 0
        self._tools = 0
        self._current = ""
        self._wait: Optional[str] = None
        self._wait_kind = ""
        self._cost = 0.0
        self._seen_usage: set[str] = set()
        self._usage_dir: Optional[Path] = None
        self._pending_edit: Optional[asyncio.Task] = None
        self._reminder_task: Optional[asyncio.Task] = None
        self._reminded = False
        self._closed = False

    # -- lazy binding ------------------------------------------------------

    def try_match(self, session_id: str) -> bool:
        """Bind to *session_id* if it reverse-resolves to our session_key."""
        if self.session_id is not None:
            return session_id == self.session_id
        if not self._key_resolver or not self.session_key:
            return False
        if session_id in self._no_match:
            return False
        try:
            key = self._key_resolver(session_id)
        except Exception:
            key = None
        if key and key == self.session_key:
            self.session_id = session_id
            return True
        if len(self._no_match) < 256:
            self._no_match.add(session_id)
        return False

    # -- event folding (sync; runs inside the feed writer) -----------------

    def on_feed_event(self, event: Dict[str, Any]) -> None:
        """Fold one feed event; schedule a (throttled) bubble edit. Never raises."""
        try:
            if self._closed:
                return
            kind = event.get("type") or ""
            data = event.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            if kind == "tool_started":
                self._tools += 1
                name = data.get("action_name") or data.get("tool_name") or "tool"
                self._current = f"→ {name}"
            elif kind == "llm_started":
                self._current = "thinking…"
            elif kind == "step":
                step = event.get("step") or data.get("iteration") or 0
                if isinstance(step, int):
                    self._step = max(self._step, step)
            elif kind == "awaiting_approval":
                self._wait = ("⏸ Waiting for your approval — reply /pending "
                              "(or `polyrob owner pending`)")
                self._wait_kind = "approval"
                self._start_reminder()
            elif kind == "approval_resolved":
                self._clear_wait("approval")
            elif kind == "retry_wait":
                delay = data.get("delay_sec")
                tail = f" in {delay:.0f}s" if isinstance(delay, (int, float)) else ""
                self._wait = f"↻ {data.get('reason') or 'provider'} — retrying{tail}"
                self._wait_kind = "retry"
            elif kind == "compaction_started":
                self._wait = "📦 Compacting context…"
                self._wait_kind = "compacting"
            elif kind == "compaction_finished":
                self._clear_wait("compacting")
            elif kind in ("tool_execution", "llm_request"):
                # a completed span; the next start overwrites _current
                if kind == "tool_execution" and self._wait_kind == "retry":
                    self._clear_wait("retry")
            else:
                return
            self._schedule_edit(immediate=kind in _WAIT_KINDS)
        except Exception:
            logger.debug("progress tracker event fold failed", exc_info=True)

    def _clear_wait(self, wait_kind: str) -> None:
        if self._wait_kind == wait_kind:
            self._wait = None
            self._wait_kind = ""
            self._stop_reminder()

    # -- composition -------------------------------------------------------

    def compose_text(self) -> str:
        if self._wait:
            return self._wait
        elapsed = max(0.0, self._clock() - self._started_at)
        parts: List[str] = ["⚙️"]
        if self._step:
            parts.append(f"step {self._step}")
        if self._current:
            parts.append(self._current)
        if self._tools:
            parts.append(f"{self._tools} tool{'s' if self._tools != 1 else ''}")
        parts.append(f"{elapsed:.0f}s")
        self._poll_cost()
        if self._cost > 0:
            parts.append(f"${self._cost:.2f}")
        if len(parts) == 2:  # bare "⚙️ · 3s" start — keep the legacy wording
            return "⚙️ Working…"
        return f"{parts[0]} " + " · ".join(parts[1:])

    def _poll_cost(self) -> None:
        """Incrementally sum cost_estimate from the session's llm_usage records."""
        try:
            if self.session_id is None:
                return
            if self._usage_dir is None:
                from agents.task.path import pm
                self._usage_dir = Path(pm().get_subdir(self.session_id, "data")) / "llm_usage"
            if not self._usage_dir.is_dir():
                return
            for path in sorted(self._usage_dir.glob("llm_usage_*.json")):
                if path.name in self._seen_usage:
                    continue
                self._seen_usage.add(path.name)
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                cost = record.get("cost_estimate")
                if isinstance(cost, (int, float)) and cost > 0:
                    self._cost += float(cost)
        except Exception:
            pass

    # -- edit scheduling (throttled; loop-thread only) ---------------------

    def _schedule_edit(self, *, immediate: bool = False) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not on a loop thread — no live surface to edit
        now = self._clock()
        if immediate or (now - self._last_edit_at) >= self._interval:
            self._last_edit_at = now
            loop.create_task(self._do_edit())
        elif self._pending_edit is None or self._pending_edit.done():
            remaining = self._interval - (now - self._last_edit_at)
            self._pending_edit = loop.create_task(self._delayed_edit(remaining))

    async def _delayed_edit(self, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.05, delay))
            if self._closed:
                return
            self._last_edit_at = self._clock()
            await self._do_edit()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("progress delayed edit failed", exc_info=True)

    async def _do_edit(self) -> None:
        try:
            if self._closed:
                return
            text = self.compose_text()
            if text and text != self._last_text:
                self._last_text = text
                await self._reporter.stage(text)
        except Exception:
            logger.debug("progress edit failed", exc_info=True)

    # -- approval reminder (≤1 per wait) -----------------------------------

    def _start_reminder(self) -> None:
        if self._reminded or (self._reminder_task and not self._reminder_task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reminder_task = loop.create_task(self._remind_later())

    def _stop_reminder(self) -> None:
        if self._reminder_task and not self._reminder_task.done():
            self._reminder_task.cancel()
        self._reminder_task = None

    async def _remind_later(self) -> None:
        try:
            await asyncio.sleep(self._reminder_sec)
            if self._closed or self._wait_kind != "approval" or self._reminded:
                return
            self._reminded = True
            self._last_text = ""  # force the edit through the text-dedup
            self._wait = ("⏸ Still waiting for your approval — the run is blocked. "
                          "Reply /pending (or `polyrob owner pending`).")
            await self._do_edit()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("approval reminder failed", exc_info=True)

    # -- teardown ----------------------------------------------------------

    def close(self) -> None:
        """Detach + cancel timers. Idempotent; called at turn end."""
        self._closed = True
        if self._pending_edit and not self._pending_edit.done():
            self._pending_edit.cancel()
        self._stop_reminder()
        detach_tracker(self)


# ---------------------------------------------------------------------------
# Process-wide dispatch (ONE feed subscriber, per-session tracker map)
# ---------------------------------------------------------------------------

_by_session: Dict[str, TurnProgressTracker] = {}
_unbound: List[TurnProgressTracker] = []
_installed = False


def attach_tracker(tracker: TurnProgressTracker) -> None:
    """Register a turn's tracker and ensure the feed subscriber is installed."""
    _ensure_subscribed()
    if tracker.session_id:
        _by_session[tracker.session_id] = tracker
    else:
        _unbound.append(tracker)


def detach_tracker(tracker: TurnProgressTracker) -> None:
    if tracker.session_id and _by_session.get(tracker.session_id) is tracker:
        _by_session.pop(tracker.session_id, None)
    try:
        _unbound.remove(tracker)
    except ValueError:
        pass


def _dispatch(session_id: str, event: Dict[str, Any]) -> None:
    tracker = _by_session.get(session_id)
    if tracker is None and _unbound:
        for candidate in list(_unbound):
            if candidate.try_match(session_id):
                _unbound.remove(candidate)
                _by_session[session_id] = candidate
                tracker = candidate
                break
    if tracker is not None:
        tracker.on_feed_event(event)


def _ensure_subscribed() -> None:
    global _installed
    if _installed:
        return
    from agents.task.telemetry.service import ProductTelemetry
    ProductTelemetry.add_feed_subscriber(_dispatch)
    _installed = True


def _reset_for_tests() -> None:
    global _installed
    _by_session.clear()
    _unbound.clear()
    if _installed:
        try:
            from agents.task.telemetry.service import ProductTelemetry
            ProductTelemetry.remove_feed_subscriber(_dispatch)
        except Exception:
            pass
    _installed = False
