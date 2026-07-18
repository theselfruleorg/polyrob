"""streaming.py — the live streaming response box for the POLYROB CLI (Phase 3).

Two pieces live here:

``ResponseBox``
    A Rich ``Live`` "rob" box that accumulates LLM output deltas and repaints.
    It is written to be **1-or-N-chunk safe** (proposal §4.2): the agent's
    ``astream`` currently yields the whole answer in a single chunk, so the box
    must look identical whether it receives one delta or many — append + repaint
    either way.  When real per-provider streaming lands (Phase 6) the same box
    fills token-by-token with no changes here.

    Defensive by design:
    - All mutations funnel through one lock-guarded ``append`` so a delta arriving
      on the agent-loop thread can't race a ``finalize`` on the REPL thread.
    - If a Rich ``Live`` can't be started (non-TTY, no console, or any Rich
      error) the box silently degrades to **buffer-only** mode: deltas accumulate
      and the full text is returned by ``finalize`` for the caller to print once.
      It never fights prompt_toolkit's ``patch_stdout`` because Live is only
      started when explicitly given a console and the start succeeds.

``make_stream_callback``
    Builds the orchestrator-level ``_on_stream_chunk`` callback
    ``async (session_id, agent_id, chunk, step)`` that routes a chunk into a
    renderer's ``on_stream_delta`` — filtering to the **main** agent so a
    ``delegate_task`` sub-agent's stream can't interleave into the box.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Awaitable, Callable, List, Optional

from rich.text import Text

from cli.ui.theme import ICONS, style

_log = logging.getLogger(__name__)


class ResponseBox:
    """A 1-or-N-chunk-safe live response box backed by an optional Rich ``Live``.

    Args:
        console:  A Rich ``Console`` to drive the live box on.  When ``None``
                  (or when Live can't start) the box runs in buffer-only mode
                  and ``finalize`` returns the accumulated text for the caller
                  to print.
        title:    The panel title for the live box (defaults to the resolved
                  instance name, e.g. ``"rob"``).

    Lifecycle:
        ``append(delta)``  → opens the Live on the first delta, accumulates,
                             repaints.  Thread-safe.
        ``finalize()``     → stops the Live (if running) and returns the full
                             accumulated text.  Idempotent.  The caller is
                             responsible for printing the finalized static block
                             (the renderer does this via ``answer_block``) so the
                             answer persists in scrollback.

    The box deliberately does NOT print the finalized static block itself: the
    renderer owns the canonical answer block (``blocks.answer_block``) and the
    double-render guard, so finalize hands the text back rather than printing.
    """

    def __init__(self, console: Optional[Any] = None, *, title: Optional[str] = None) -> None:
        self._console = console
        # Default to the resolved instance name (not a hardcoded "rob") so a renamed
        # instance's streaming box matches its bubbles/banner.
        if title is None:
            from cli.ui.identity import agent_display_name
            title = agent_display_name()
        self._title = title
        self._lock = threading.RLock()
        self._chunks: List[str] = []
        self._live: Optional[Any] = None
        self._live_failed = False
        self._finalized = False
        self._got_chunk = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def text(self) -> str:
        """The full text accumulated so far."""
        with self._lock:
            return "".join(self._chunks)

    @property
    def received_chunk(self) -> bool:
        """True once at least one delta has been appended this box's lifetime."""
        with self._lock:
            return self._got_chunk

    @property
    def is_live(self) -> bool:
        """True when a Rich ``Live`` is currently running (TTY path)."""
        with self._lock:
            return self._live is not None

    # ------------------------------------------------------------------
    # Mutation (single funnel — thread-safe)
    # ------------------------------------------------------------------

    def append(self, delta: str) -> None:
        """Append *delta* and repaint.

        Safe to call from any thread (the stream callback fires on the agent
        loop thread).  The first call opens the Live box when a console is
        available; subsequent calls repaint it.  All errors are swallowed so a
        rendering hiccup can never break the agent run.
        """
        if not delta:
            return
        with self._lock:
            if self._finalized:
                # A late chunk after finalize: keep the text coherent but don't
                # try to repaint a stopped Live.
                self._chunks.append(delta)
                self._got_chunk = True
                return
            self._chunks.append(delta)
            self._got_chunk = True
            self._ensure_live()
            self._repaint()

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def finalize(self) -> str:
        """Stop the Live (if any) and return the accumulated text.

        Idempotent: a second call returns the same text and is a no-op on the
        Live.  The caller prints the static finalized block (so it persists in
        scrollback) — this method does not print.
        """
        with self._lock:
            self._finalized = True
            if self._live is not None:
                try:
                    self._live.stop()
                except Exception:  # pragma: no cover - defensive
                    pass
                self._live = None
            return "".join(self._chunks)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_live(self) -> None:
        """Start the Rich ``Live`` lazily on the first delta (best-effort)."""
        if self._live is not None or self._live_failed or self._console is None:
            return
        try:
            from rich.live import Live

            # transient=True: the live box clears itself when stopped, so the
            # renderer can print exactly one canonical static block at finalize
            # (no double-render, persists cleanly in scrollback).
            live = Live(
                self._renderable(),
                console=self._console,
                refresh_per_second=4,  # was 10; see cli/ui/activity.py for rationale
                transient=True,
            )
            live.start()
            self._live = live
        except Exception:
            # Non-TTY, no console, or any Rich failure → buffer-only degrade.
            self._live = None
            self._live_failed = True

    def _repaint(self) -> None:
        """Repaint the live box with the current text (best-effort)."""
        if self._live is None:
            return
        try:
            self._live.update(self._renderable())
        except Exception:  # pragma: no cover - defensive
            pass

    def _renderable(self) -> Any:
        """Build the live renderable for the current accumulated text.

        Mirrors ``blocks.agent_message`` (speaker mark + indented Markdown) so
        the live box and the finalized static block are visually identical —
        the handoff at finalize is seamless.

        Precondition: called only from append()/finalize() while _lock is held.
        """
        from rich.console import Group
        from rich.markdown import Markdown
        from rich.padding import Padding

        speaker = Text()
        speaker.append(f"{ICONS.speaker} ", style=style("speaker_dot"))
        speaker.append(self._title, style=style("speaker_name"))
        body = Padding(Markdown("".join(self._chunks)), (0, 0, 0, 2))
        return Group(Text(""), speaker, body)


# ----------------------------------------------------------------------
# Orchestrator stream-callback bridge
# ----------------------------------------------------------------------


StreamCallback = Callable[[str, str, str, int], Awaitable[None]]


def make_stream_callback(
    renderer: Any,
    *,
    main_agent_id: Optional[Callable[[], str]] = None,
) -> StreamCallback:
    """Build the orchestrator-level ``_on_stream_chunk`` callback.

    The returned coroutine matches the orchestrator seam signature exactly
    (verified in ``agents/task/session/feed.py::_register_stream_callback``):
    ``async (session_id, agent_id, chunk, step) -> None``.

    It routes each chunk into ``renderer.on_stream_delta(chunk)`` — but only for
    the **main** agent.  Sub-agents spawned by ``delegate_task`` also stream;
    interleaving their tokens into the box would corrupt the answer, so they are
    dropped.  ``main_agent_id`` is a late-bound accessor (the id may not be known
    at wire-up time); when it returns "" (unknown) we accept all chunks (the
    common single-agent REPL case).

    The callback is fully fail-open: any renderer error is swallowed so a
    rendering hiccup can never fail the agent run.

    Args:
        renderer:       A ``Renderer`` exposing ``on_stream_delta(str)``.
        main_agent_id:  Optional zero-arg accessor returning the main agent's id
                        (e.g. ``lambda: state.main_agent_id``).  When omitted or
                        returning "", all chunks are accepted.
    """

    async def _callback(session_id: str, agent_id: str, chunk: str, step: int) -> None:
        try:
            if main_agent_id is not None:
                expected = main_agent_id() or ""
                if expected and agent_id and agent_id != expected:
                    return  # sub-agent stream — don't interleave into the box
            renderer.on_stream_delta(chunk)
        except Exception as exc:
            # Streaming is non-critical; never propagate into the agent loop.
            _log.debug("stream callback render error (ignored): %s", exc)

    return _callback
