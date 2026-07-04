"""SessionState accumulator for the POLYROB CLI renderer (Phase 1).

``SessionState`` is the single source of truth the renderer reads from.
It is updated in two ways:

1. **Push** — ``update(event)`` is called for every incoming ``RenderEvent``.
2. **Pull** — ``poll(agent)`` is called once per turn, reading live metrics
   from the ``message_manager`` that are not available via the feed.

Design notes:
- No I/O; pure in-memory accumulation.
- Elapsed time uses ``time.monotonic()`` captured at construction (session
  start) so it doesn't drift with wall-clock skew.
- Cost is a rolling sum of ``LLMCall.cost_estimate`` values.  It is an
  *estimate* — the authoritative total is in ``LLMUsageTracker``, available
  on demand via ``/usage`` (Phase 4).
- All numeric fields default to zero / None so the renderer can always
  format them safely.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from cli.ui.events import (
    AgentEnd,
    AgentRegistration,
    ErrorEvent,
    IterationDone,
    LLMCall,
    RenderEvent,
    SessionDone,
    SessionStart,
    Step,
    ToolExec,
)
from cli.ui.lifecycle import TurnLifecycle


class SessionState:
    """Mutable accumulator of session metrics for the CLI renderer.

    Attributes:
        model:            LLM model name (set from SessionStart).
        provider:         LLM provider name (set from first LLMCall).
        started_at:       ``time.monotonic()`` at construction.
        status:           Human-readable status string.

        tokens_in:        Accumulated prompt tokens.
        tokens_out:       Accumulated completion tokens.
        tokens_cached:    Placeholder (not emitted by current feed; kept for
                          future compatibility with cache-token reporting).
        tokens_total:     Accumulated ``token_count`` (may differ from
                          in+out due to cache accounting).
        cost_estimate_total: Running sum of ``LLMCall.cost_estimate`` values.

        step:             Current step number.
        max_steps:        Maximum steps (not in feed; settable externally).
        tool_calls:       Count of tool-execution events seen.
        errors:           Count of error events seen.

        ctx_percent:      Context window usage 0–100 (polled).
        ctx_tokens:       Raw token count in context (polled).
        ctx_max:          Max input tokens (polled).
        compactions:      Number of compaction events so far (polled).
    """

    def __init__(self, *, clock: Optional[Any] = None) -> None:
        self._clock = clock or time.monotonic
        self.model: str = ""
        self.provider: str = ""
        # ``started_at`` is the SESSION AGE anchor (used by /session). The live bar
        # shows the per-turn WORK clock via ``lifecycle.active_elapsed()`` instead —
        # session age must never be presented as "elapsed" work time.
        self.started_at: float = self._clock()
        self.status: str = "starting"

        # The single source of truth for "is a turn active, and for how long".
        # The bar's clock + status word derive from this; feed events do NOT
        # mutate it (only the turn runner does, via begin_turn/end_turn).
        self.lifecycle: TurnLifecycle = TurnLifecycle(clock=self._clock)

        # Token accounting (accumulated from LLMCall events)
        self.tokens_in: int = 0
        self.tokens_out: int = 0
        self.tokens_cached: int = 0
        self.tokens_total: int = 0
        self.cost_estimate_total: float = 0.0

        # Step / progress
        self.step: int = 0
        self.max_steps: Optional[int] = None
        self.tool_calls: int = 0
        self.errors: int = 0

        # Context window metrics (polled from message_manager each turn)
        self.ctx_percent: float = 0.0
        self.ctx_tokens: int = 0
        self.ctx_max: int = 0
        self.compactions: int = 0

        # Sub-agent grouping: the first registered agent is the "main" agent;
        # any other agent_id/agent_name seen is a sub-agent (delegate_task spawn).
        # Both id and name are stored because step events carry only agent_name
        # (the real AgentStepFormatter never emits agent_id in the step dict).
        self.main_agent_id: str = ""
        self.main_agent_name: str = ""
        # True when the most-recent Step belonged to a sub-agent. tool_execution
        # events carry no agent identity, so the renderer correlates a tool
        # result to the step that declared it to suppress sub-agent tool churn
        # from the default (non-verbose) transcript.
        self.last_step_sub_agent: bool = False

        # Live-info (D3): count of in-flight delegate_task sub-agents (fed by the
        # orchestrator sub-agent lifecycle hooks the REPL registers) and the name
        # of the most-recent tool action (the in-flight tool for the status line).
        self.subagents_active: int = 0
        self.last_tool: str = ""
        # Slow-polled, cached autonomy snapshot for the 2nd status line (D4).
        # {"goals": int, "cron": int, "review": bool}. None/empty → line hidden.
        # NEVER read the goal/cron SQLite stores on the hot repaint path.
        self.autonomy_snapshot: Optional[dict] = None

        # poll_usage() bookkeeping: filenames already aggregated so each poll
        # only reads NEW llm_usage files (cheap, incremental).
        self._seen_usage_files: set[str] = set()

    # ------------------------------------------------------------------
    # Live sub-agent counter (fed by the REPL's orchestrator hooks)
    # ------------------------------------------------------------------

    def note_subagent_start(self) -> None:
        """A delegate_task sub-agent began (orchestrator start hook)."""
        self.subagents_active += 1

    def note_subagent_end(self) -> None:
        """A delegate_task sub-agent finished (orchestrator end hook). Floored at 0."""
        self.subagents_active = max(0, self.subagents_active - 1)

    # ------------------------------------------------------------------
    # Push: event accumulation
    # ------------------------------------------------------------------

    def update(self, event: RenderEvent) -> None:
        """Accumulate state from one ``RenderEvent``."""
        # Extension seam (D2): a registered event mutates state via its spec's
        # ``apply`` — no isinstance branch needed here for new core events.
        from cli.ui.event_registry import RegisteredEvent, get_spec
        if isinstance(event, RegisteredEvent):
            spec = get_spec(event.type)
            if spec is not None:
                spec.apply(self, event)
            return

        # NOTE: feed events NEVER drive ``status`` — the TurnLifecycle (begin_turn/
        # end_turn at the turn seam) is the single source of truth for the status
        # word + work clock. A background autonomy turn emits the same feed events
        # as a user turn, so letting them set ``status`` is exactly what flipped the
        # idle bar to "running". Events here only accumulate counters/metadata.
        if isinstance(event, SessionStart):
            if event.model_name:
                self.model = event.model_name

        elif isinstance(event, LLMCall):
            if event.provider and not self.provider:
                self.provider = event.provider
            if event.model_name and not self.model:
                self.model = event.model_name
            if event.prompt_tokens is not None:
                self.tokens_in += event.prompt_tokens
            if event.completion_tokens is not None:
                self.tokens_out += event.completion_tokens
            if event.token_count is not None:
                self.tokens_total += event.token_count
            if event.cost_estimate is not None:
                self.cost_estimate_total += event.cost_estimate

        elif isinstance(event, Step):
            self.step = max(self.step, event.step)
            data = event.raw.get("data", {}) or {}
            identity = str(
                data.get("agent_name")
                or event.raw.get("agent_name")
                or data.get("agent_id")
                or event.raw.get("agent_id")
                or ""
            )
            self.last_step_sub_agent = self.is_sub_agent(identity)

        elif isinstance(event, ToolExec):
            self.tool_calls += 1
            if event.action_name:
                self.last_tool = event.action_name

        elif isinstance(event, ErrorEvent):
            self.errors += 1

        elif isinstance(event, IterationDone):
            # No status write — the lifecycle settles the turn on respond() return,
            # not on a feed "done" event (so a planning-only turn still settles).
            pass

        elif isinstance(event, SessionDone):
            # Success/failure colors the turn-summary residue via the renderer's
            # event buffer (turn_failed()), not a status write here.
            pass

        elif isinstance(event, AgentRegistration):
            # First registration = main agent (used for sub-agent grouping).
            # Store both id and name: step events carry only agent_name, so
            # identity comparison must work on names too.
            if not self.main_agent_id and event.agent_id:
                self.main_agent_id = event.agent_id
            if not self.main_agent_name and event.agent_name:
                self.main_agent_name = event.agent_name
            if event.model_name and not self.model:
                self.model = event.model_name

        elif isinstance(event, AgentEnd):
            # Informational; sub-agent failures bump the error count.
            if event.errors:
                self.errors += len(event.errors)

    def is_sub_agent(self, identity: str) -> bool:
        """True if *identity* belongs to a sub-agent (not the main agent).

        *identity* may be an agent_id OR an agent_name — a match on either
        is treated as "this is the main agent".  Step events from the real
        ``AgentStepFormatter`` carry only ``agent_name`` (never ``agent_id``),
        so name-based comparison is the primary path.

        Before any registration is seen (both ``main_agent_id`` and
        ``main_agent_name`` empty) nothing is treated as a sub-agent.
        """
        if not identity:
            return False
        if not self.main_agent_id and not self.main_agent_name:
            return False
        # Match on id OR name — whichever the caller supplies.
        if self.main_agent_id and identity == self.main_agent_id:
            return False
        if self.main_agent_name and identity == self.main_agent_name:
            return False
        return True

    # ------------------------------------------------------------------
    # Pull: live metrics from the agent's message_manager
    # ------------------------------------------------------------------

    def reset_after_clear(self) -> None:
        """Reset per-conversation live metrics after /clear wiped the history.

        The context window + step counter are now stale, so zero them (and the
        per-turn tallies). Cumulative session tokens/cost are PRESERVED (real spend)
        so /status still reflects the session's total.
        """
        self.step = 0
        self.tool_calls = 0
        self.errors = 0
        self.ctx_percent = 0.0
        self.ctx_tokens = 0
        self.compactions = 0

    def poll(self, agent: Any) -> None:
        """Pull live context-window metrics from *agent*'s message_manager.

        *agent* must expose:
            agent.message_manager.get_context_usage_percent() -> float
            agent.message_manager.history.total_tokens -> int
            agent.message_manager.max_input_tokens -> int
            agent.message_manager._compaction_count -> int

        Missing attributes are silently ignored (the call is best-effort).

        Args:
            agent: A running ``Agent`` instance (or any object with the
                expected ``message_manager`` attributes).
        """
        try:
            mm = agent.message_manager
        except Exception:
            return

        try:
            self.ctx_percent = mm.get_context_usage_percent()
        except Exception:
            pass

        try:
            self.ctx_tokens = mm.history.total_tokens
        except Exception:
            pass

        try:
            self.ctx_max = mm.max_input_tokens
        except Exception:
            pass

        try:
            self.compactions = mm._compaction_count
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Pull: authoritative live tokens/cost from the session's llm_usage dir
    # ------------------------------------------------------------------

    def poll_usage(self, session_dir: Path) -> None:
        """Aggregate tokens + cost from new ``data/llm_usage/*.json`` files.

        This is the LIVE source of tokens/cost in the local CLI path:
        ``capture_llm_usage`` skips the push feed (to avoid double-counting)
        and instead writes one record per LLM call to
        ``<session_dir>/data/llm_usage/llm_usage_<ts>.json`` with the same
        shape the formatter uses (``prompt_tokens``, ``completion_tokens``,
        ``token_count``, ``cost_estimate``).

        Each call only reads files not seen on a previous poll, so it is cheap
        to call every turn (and on the toolbar timer).  Best-effort: malformed
        or partially-written files are skipped silently.

        Args:
            session_dir: The session root (containing ``data/llm_usage/``).
        """
        try:
            usage_dir = Path(session_dir) / "data" / "llm_usage"
            if not usage_dir.is_dir():
                return
            for path in sorted(usage_dir.glob("llm_usage_*.json")):
                name = path.name
                if name in self._seen_usage_files:
                    continue
                # Mark as seen up-front: a file that fails to parse once (e.g.
                # mid-write) is unlikely to be the live source of truth, and we
                # never want to re-add its tokens on a later poll.
                self._seen_usage_files.add(name)
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                self._apply_usage_record(record)
        except Exception:
            # poll_usage must never raise into the render loop.
            return

    def _apply_usage_record(self, record: dict[str, Any]) -> None:
        """Add one llm_usage record's tokens + cost into the running totals."""
        prompt = record.get("prompt_tokens")
        completion = record.get("completion_tokens")
        total = record.get("token_count")
        cost = record.get("cost_estimate")
        if isinstance(prompt, (int, float)):
            self.tokens_in += int(prompt)
        if isinstance(completion, (int, float)):
            self.tokens_out += int(completion)
        if isinstance(total, (int, float)):
            self.tokens_total += int(total)
        if isinstance(cost, (int, float)):
            self.cost_estimate_total += float(cost)
        provider = record.get("provider")
        if provider and not self.provider:
            self.provider = str(provider)
        model = record.get("model_name")
        if model and not self.model:
            self.model = str(model)

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    def elapsed(self) -> float:
        """Seconds elapsed since this ``SessionState`` was constructed."""
        return time.monotonic() - self.started_at
