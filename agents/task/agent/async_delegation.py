"""Durable async delegation (UP-12) — `delegate_task(background=true)`.

POLYROB's children are `asyncio` coroutines on the agent's event loop, so the
async-equivalent of Reference' daemon thread pool is a manager-owned set of detached
`asyncio.Task`s. `AsyncDelegationRegistry.dispatch()` records the job under a single
lock (capacity check + insert is atomic — no TOCTOU where two dispatches both pass the
cap) and detaches `asyncio.create_task(self._run_and_deliver(...))`, returning a handle
immediately. The detached task runs `SubAgentManager.run_subtask(...)` (reusing its
concurrency semaphore + timeout + output extraction) and, on completion (success /
error / timeout), delivers exactly one self-contained completion block back into the
originating session via an injected `deliver` callback.

The completion re-enters the parent as a NEW turn through the existing HITL
user-message ingress (orchestrator.submit_user_message → hitl_manager queue →
run-loop drain → inject_user_guidance), so strict message-role alternation and the
prompt cache stay intact — we never splice a result between a tool result and an
assistant message.

Scope: durable ACROSS THE TURN (a background child outlives the dispatching turn and
re-enters a later turn of the same in-process session). NOT across process restart —
that is the scheduler's job (cron/). Single-worker: a completion for a session that
moved workers is parked/logged by submit_user_message, never forwarded (P2-8).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _frame_delegation_payload(result_text: str) -> str:
    """Frame an untrusted delegation result as DATA (P1-3).

    Wraps the child output in ``<untrusted_tool_result>`` delimiters and defangs any
    literal ``</delegation-result>`` in the payload so it cannot close the outer
    envelope. Fail-open: framing is defense-in-depth, never a hard dependency.
    """
    text = result_text if isinstance(result_text, str) else str(result_text)
    # Defang the envelope close tag (case-insensitive) so injected output can't break out.
    import re
    text = re.sub(r"</\s*delegation-result\s*>", "<!-- /delegation-result -->", text,
                  flags=re.IGNORECASE)
    try:
        from agents.task.agent.core.untrusted_wrap import wrap_untrusted
        return wrap_untrusted("async_delegation", text)
    except Exception:
        return text  # fail-open

# Keep at most this many finished records so the dict doesn't grow unbounded.
_MAX_RETAINED_COMPLETED = 50


@dataclass
class DelegationRecord:
    delegation_id: str
    goal: str
    profile: str
    parent_agent_id: Optional[str] = None
    status: str = "running"  # running | completed | error | timeout
    dispatched_at: float = 0.0
    completed_at: Optional[float] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)


# A deliver callback: (DelegationRecord, result_text) -> awaitable. Injected so the
# registry is unit-testable without an orchestrator.
DeliverFn = Callable[[DelegationRecord, str], Awaitable[None]]


class AsyncDelegationRegistry:
    """Owns detached background delegations + their lifecycle for one session."""

    def __init__(self, manager: Any, deliver: DeliverFn, *,
                 clock: Callable[[], float] = time.time,
                 store: Any = None, session_id: str = "", user_id: str = ""):
        self._manager = manager
        self._deliver = deliver
        self._clock = clock
        self._lock = asyncio.Lock()
        self._records: Dict[str, DelegationRecord] = {}
        self._counter = 0
        # Optional AutonomyStateStore write-through — restart-durable
        # record of dispatched/terminal delegations (recovery in autonomy_state.py).
        # Fail-open: any store error degrades to the legacy in-memory behavior.
        self._store = store
        self._session_id = session_id
        self._user_id = user_id
        # Counter seeding (past persisted ids) is LAZY — done under the dispatch
        # lock on first use, so constructing an orchestrator costs zero sqlite I/O.
        self._counter_seeded = store is None or not session_id

    # -- introspection (for a future delegation_status tool; out of v1 scope) --
    def active_count(self) -> int:
        return sum(1 for r in self._records.values() if r.status == "running")

    def list(self) -> List[DelegationRecord]:
        return list(self._records.values())

    def _next_id(self) -> str:
        self._counter += 1
        return f"deleg_{self._counter:04d}"

    async def dispatch(
        self,
        *,
        goal: str,
        profile: str = "executor",
        max_steps: int = 30,
        parent_agent_id: str,
        caller_is_sub: bool = False,
    ) -> Dict[str, Any]:
        """Capacity-check + record + detach. Returns a handle dict immediately.

        At capacity (MAX_ASYNC_SUB_AGENTS background slots live) the dispatch is
        REJECTED (not queued) so a runaway model can't pile up background work.
        """
        from agents.task.constants import TimeoutConfig

        cap = TimeoutConfig.get_max_async_sub_agents()
        async with self._lock:
            if not self._counter_seeded:
                try:
                    self._counter = max(self._counter,
                                        self._store.max_counter(self._session_id))
                except Exception:
                    logger.warning("delegation counter seed failed", exc_info=True)
                self._counter_seeded = True
            if self.active_count() >= cap:
                return {
                    "status": "rejected",
                    "error": (
                        f"Background delegation capacity reached ({cap} live). Run this "
                        "synchronously (background=false) or wait for a slot to free."
                    ),
                }
            delegation_id = self._next_id()
            rec = DelegationRecord(
                delegation_id=delegation_id,
                goal=goal,
                profile=profile,
                parent_agent_id=parent_agent_id,
                dispatched_at=self._clock(),
            )
            self._records[delegation_id] = rec
            if self._store is not None:
                try:
                    self._store.record_dispatched(
                        session_id=self._session_id, user_id=self._user_id,
                        delegation_id=delegation_id, goal=goal, profile=profile,
                        parent_agent_id=parent_agent_id,
                        dispatched_at=rec.dispatched_at,
                    )
                except Exception:
                    logger.warning("delegation durable write failed", exc_info=True)
            rec.task = asyncio.create_task(
                self._run_and_deliver(
                    rec, goal=goal, profile=profile, max_steps=max_steps,
                    parent_agent_id=parent_agent_id, caller_is_sub=caller_is_sub,
                )
            )
            self._prune_completed()
        return {"status": "dispatched", "delegation_id": delegation_id}

    async def _run_and_deliver(
        self,
        rec: DelegationRecord,
        *,
        goal: str,
        profile: str,
        max_steps: int,
        parent_agent_id: str,
        caller_is_sub: bool,
    ) -> None:
        """Run the child to completion and deliver exactly one completion block."""
        status = "completed"
        result_text = ""
        try:
            result = await self._manager.run_subtask(
                task=goal,
                parent_agent_id=parent_agent_id,
                profile_id=profile,
                max_steps=max_steps,
                is_parent_sub_agent=caller_is_sub,
            )
            if getattr(result, "success", False):
                status = "completed"
                result_text = self._extract_output(result)
            else:
                status = "error"
                result_text = getattr(result, "error", None) or "Background task failed."
        except asyncio.TimeoutError:
            status = "timeout"
            result_text = "Background task timed out."
        except asyncio.CancelledError:
            # Don't deliver on cancellation (shutdown); re-raise so the loop sees it.
            # Set the LOCAL `status` (not rec.status) — the finally below writes
            # rec.status = status, so assigning rec.status here would be clobbered
            # back to "completed". After the re-raise, the delivery code below is
            # skipped (finally runs, then the exception propagates).
            status = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001 — fail-loud-log, never crash the loop
            status = "error"
            result_text = f"Background task raised: {e}"
            logger.error("async delegation %s crashed: %s", rec.delegation_id, e, exc_info=True)
        finally:
            rec.status = status
            rec.completed_at = self._clock()
            if self._store is not None:
                try:
                    self._store.record_terminal(
                        self._session_id, rec.delegation_id, status=status,
                        completed_at=rec.completed_at, result_text=result_text,
                    )
                except Exception:
                    logger.warning("delegation terminal write failed", exc_info=True)

        block = self._format_completion_block(rec, status, result_text)
        try:
            await self._deliver(rec, block)
        except Exception as e:  # noqa: BLE001 — delivery is best-effort (e.g. queue full)
            logger.error(
                "async delegation %s finished (%s) but delivery failed: %s",
                rec.delegation_id, status, e,
            )

    @staticmethod
    def _extract_output(result: Any) -> str:
        # SubAgentResult.output_text property gives the string form; fall back to str().
        for attr in ("output_text", "output"):
            val = getattr(result, attr, None)
            if isinstance(val, str) and val:
                return val
            if val is not None and not isinstance(val, str):
                return str(val)
        return "(no output)"

    @staticmethod
    def _format_completion_block(rec: DelegationRecord, status: str, result_text: str) -> str:
        """Self-contained block so a parent deep in unrelated context knows why this
        arrived (mirrors Reference' task-source block).

        P1-3: the child's ``result_text`` is UNTRUSTED — it can quote injected web/MCP
        content verbatim (the child's tool results were wrapped, but its final output
        is not). Frame it as DATA (mirrors the self-wake rail's ``format_self_wake``):
        the ``<delegation-result>`` envelope + trusted footer stay outside, the payload
        goes inside an ``<untrusted_tool_result>`` block, and a literal
        ``</delegation-result>`` in the payload is defanged so it can't break the
        envelope.
        """
        emoji = {"completed": "✅", "error": "❌", "timeout": "⏱️"}.get(status, "ℹ️")
        safe_result = _frame_delegation_payload(result_text)
        return (
            f"<delegation-result delegation_id=\"{rec.delegation_id}\" status=\"{status}\">\n"
            f"{emoji} A background task you dispatched has finished.\n\n"
            f"**delegation_id:** {rec.delegation_id}\n"
            f"**goal:** {rec.goal}\n"
            f"**status:** {status}\n\n"
            f"**result:**\n{safe_result}\n\n"
            "Use this result, or re-dispatch if the situation has changed.\n"
            "</delegation-result>"
        )

    def _prune_completed(self) -> None:
        done = [r for r in self._records.values() if r.status != "running"]
        if len(done) <= _MAX_RETAINED_COMPLETED:
            return
        done.sort(key=lambda r: r.completed_at or 0.0)
        for rec in done[: len(done) - _MAX_RETAINED_COMPLETED]:
            self._records.pop(rec.delegation_id, None)
