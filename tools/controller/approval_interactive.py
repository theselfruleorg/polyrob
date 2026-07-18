"""Interactive CLI approval provider (P0-F; owner-UX P2 T5 approval ladder).

Wires the (mechanism-only) ApprovalProvider seam to an actual owner prompt. On a gated
action the provider prints the action + a params digest and asks the operator to pick
one of an approval ladder:

  [o]nce         - approve this call only.
  [s]ession      - approve + remember this ACTION for the rest of THIS provider's
                   lifetime (process-local, in-memory only, no disk writes).
  [a]lways-allow - approve now AND, if the action is pref-added (present in
                   ``pref_gated_actions``), queue a GUARDED removal proposal
                   (``core.prefs.propose_pref_change(op="remove_entry")``) for
                   owner review via ``/pending`` — an interactive keystroke
                   mid-task must never silently widen policy on its own. If the
                   action is env/posture-gated instead, there is nothing to
                   propose removing (the operator/posture owns that gate), so
                   it behaves like [s]ession. Either way this call is approved
                   and remembered for the session.
  [d]eny         - deny this call only (today's plain no).
  [n]ever        - deny + append the action to the ``approvals.deny`` pref
                   (tightening a denylist is always safe -> written immediately,
                   no owner-review gate needed).

Unrecognized input re-prompts once, then fails CLOSED (deny) — never guesses.

The blocking input runs in a worker thread (``asyncio.to_thread``) so it yields the
event loop; the whole call is bounded by the approval hook's ``asyncio.wait_for``.
Injectable ``input_fn`` makes it unit-testable without a TTY.

Tenant context (owner-UX P2 T5): the provider optionally takes ``user_id``/``home_dir``
at construction (threaded from ``Controller.__init__`` via
``get_approval_provider_or_deny``). Without both, [a]/[n] can't safely touch
per-tenant preferences.toml — they degrade to [s]/[d] respectively with a one-line
notice, never crash. Any prefs read/write failure (disk error, scanner unavailable,
etc.) is caught and logged; it NEVER blocks the already-decided approve/deny outcome
(fail-open on the bookkeeping, not on the decision).

⚠️ Single stdin reader (H8). A blocking ``input()`` in a worker thread cannot be
interrupted by an ``asyncio.wait_for`` timeout — cancelling the *await* leaves the thread
reading stdin. If a second gated action then prompted, it would spawn a SECOND stdin
reader and a late keystroke could be consumed by the wrong (stale, already-timed-out)
prompt. So only ONE interactive prompt owns stdin at a time: a second concurrent prompt
denies (fail-closed) instead of racing. The in-flight flag is cleared by the reader
thread itself when ``input()`` finally returns (even after the await was cancelled), so a
timed-out prompt's lingering thread can never be mistaken for a free stdin.

⚠️ Cancellation safety (P2 T5): everything the ladder does after a decision is read
(the ``_apply_decision`` bookkeeping — prefs reads/writes) runs AFTER
``asyncio.to_thread`` has already returned, i.e. AFTER the point where
``asyncio.wait_for`` could have cancelled us. A cancelled ``request()`` never reaches
``_apply_decision`` and holds no resource across the input await (the single in-flight
stdin flag is released in the reader thread's own ``finally``, unchanged from P0-F).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Dict, Optional

from tools.controller.approval import ApprovalProvider, register_approval_provider

logger = logging.getLogger(__name__)

# Set while a real blocking input() is outstanding on stdin (process-wide — there is
# only one stdin). Cleared by the reader thread's own finally, not on await-cancel.
_stdin_in_flight = threading.Event()

# Ladder input aliases -> canonical decision token. Includes the pre-ladder y/n
# spellings ("y"/"yes"/"no") as back-compat aliases so existing callers/tests
# that only knew yes/no keep working: "y"/"yes" -> once (today's yes), "no" ->
# deny (today's no; distinct from "n" == never, the new persistent-deny verb).
_LADDER_ALIASES: Dict[str, str] = {
    "o": "once", "once": "once", "y": "once", "yes": "once",
    "s": "session", "session": "session",
    "a": "always", "always": "always", "always-allow": "always", "allow": "always",
    "d": "deny", "deny": "deny", "no": "deny",
    "n": "never", "never": "never",
}


def _parse_ladder(answer: Any) -> Optional[str]:
    return _LADDER_ALIASES.get(str(answer).strip().lower())


class InteractiveCLIApprover(ApprovalProvider):
    """Prompt the local operator to approve/deny a gated action via the ladder."""

    def __init__(self, input_fn: Optional[Callable[[str], str]] = None,
                 user_id: Optional[str] = None, home_dir: Any = None) -> None:
        self._input_fn = input_fn or input  # real stdin by default; injectable for tests
        self._user_id = user_id
        self._home_dir = home_dir
        # [s]ession/[a]lways-allow auto-approve set: process-local, in-memory
        # only, cleared when this provider instance is gone. No disk writes.
        self._session_approved: set = set()

    def _has_tenant_context(self) -> bool:
        return bool(self._user_id) and self._home_dir is not None

    @staticmethod
    def _digest(params: Dict[str, Any]) -> str:
        try:
            items = list((params or {}).items())
        except Exception:
            return "(unprintable params)"
        parts = [f"{k}={str(v)[:60]}" for k, v in items[:6]]
        return "{" + ", ".join(parts) + "}"

    def _prompt(self, action_name: str, params: Dict[str, Any]) -> str:
        return (
            f"\n[approval] Allow '{action_name}'? {self._digest(params)}\n"
            "  [o]nce / [s]ession / [a]lways-allow / [d]eny / [n]ever: "
        )

    @staticmethod
    def _reprompt(action_name: str) -> str:
        return (
            f"  unrecognized answer for '{action_name}' — "
            "enter one of o/s/a/d/n: "
        )

    def _read_decision(self, input_fn: Callable[[str], str], action_name: str,
                       prompt: str) -> str:
        """Blocking (runs in a worker thread): read one answer, re-prompt once on
        an unrecognized answer, then fail CLOSED (deny) rather than guess."""
        answer = input_fn(prompt)
        decision = _parse_ladder(answer)
        if decision is not None:
            return decision
        answer2 = input_fn(self._reprompt(action_name))
        decision2 = _parse_ladder(answer2)
        if decision2 is not None:
            return decision2
        logger.warning(
            "approval ladder: unrecognized input twice for '%s' -> deny (fail-closed)",
            action_name,
        )
        return "deny"

    def _handle_always(self, action_name: str) -> None:
        """[a]lways-allow bookkeeping. Never raises — any failure here is logged
        and falls open to session-scoped only (the approve decision for THIS
        call is already made independently of this method)."""
        if not self._has_tenant_context():
            print(f"[approval] '{action_name}': no tenant context — session-scoped only")
            return
        try:
            from tools.controller.approval import pref_gated_actions
            from core.prefs import propose_pref_change

            pref_added = action_name in pref_gated_actions(self._user_id, self._home_dir)
            if pref_added:
                ok, msg = propose_pref_change(
                    self._user_id, "approvals.require", None, self._home_dir,
                    op="remove_entry", entry=action_name,
                )
                if ok:
                    print(
                        f"[approval] '{action_name}': queued removal for owner "
                        "review (/pending)"
                    )
                else:
                    logger.warning(
                        "approval ladder: queue removal failed for '%s': %s",
                        action_name, msg,
                    )
            else:
                print(
                    f"[approval] '{action_name}': operator-controlled — "
                    "approved for this session only"
                )
        except Exception as e:
            logger.warning(
                "approval ladder: always-allow bookkeeping failed for '%s' "
                "(falling open to session-scoped only): %s", action_name, e,
            )

    def _handle_never(self, action_name: str) -> None:
        """[n]ever bookkeeping. Never raises — the deny decision for THIS call
        is already made independently of this method (fail-open on persistence,
        not on the decision)."""
        if not self._has_tenant_context():
            print(f"[approval] '{action_name}': no tenant context — session-scoped only")
            return
        try:
            from core.prefs import load_preferences, write_preference

            current = list(
                load_preferences(self._home_dir, self._user_id).get("approvals.deny", [])
                or []
            )
            if action_name in current:
                print(f"[approval] '{action_name}': already in approvals.deny")
                return
            updated = current + [action_name]
            ok, err = write_preference(
                self._home_dir, self._user_id, "approvals.deny", updated,
            )
            if ok:
                print(
                    f"[approval] '{action_name}': persisted to approvals.deny "
                    "— will never run again"
                )
            else:
                logger.warning(
                    "approval ladder: persist deny failed for '%s': %s",
                    action_name, err,
                )
        except Exception as e:
            logger.warning(
                "approval ladder: never-bookkeeping failed for '%s' "
                "(deny for this call still honored): %s", action_name, e,
            )

    def _apply_decision(self, decision: str, action_name: str) -> bool:
        if decision == "once":
            return True
        if decision == "session":
            self._session_approved.add(action_name)
            return True
        if decision == "always":
            # Remembered for the rest of the session regardless of whether the
            # removal proposal below succeeds — see module docstring.
            self._session_approved.add(action_name)
            self._handle_always(action_name)
            return True
        if decision == "deny":
            return False
        if decision == "never":
            self._handle_never(action_name)
            return False
        return False  # unreachable: _read_decision only returns known tokens

    async def request(self, action_name: str, params: Dict[str, Any], context: Any) -> bool:
        # [s]ession/[a]lways-allow short-circuit: no prompt, no disk I/O.
        if action_name in self._session_approved:
            return True

        # H8: only one interactive prompt may own stdin at a time. If a prompt is
        # already outstanding, deny (fail-closed) rather than spawn a competing reader.
        if _stdin_in_flight.is_set():
            logger.warning(
                "approval prompt already in flight -> deny '%s' (single stdin reader)",
                action_name,
            )
            return False

        prompt = self._prompt(action_name, params)
        input_fn = self._input_fn

        def _blocking_read() -> str:
            # The finally runs in THIS worker thread when the read(s) actually
            # return, so the in-flight flag stays set until the operator
            # responds — even if the awaiting coroutine was already cancelled
            # by a wait_for timeout.
            try:
                return self._read_decision(input_fn, action_name, prompt)
            finally:
                _stdin_in_flight.clear()

        _stdin_in_flight.set()
        try:
            decision = await asyncio.to_thread(_blocking_read)
        except asyncio.CancelledError:
            # Timeout/cancel: the reader thread keeps running and will clear the flag
            # when input() returns; until then new prompts deny. Propagate -> deny.
            logger.info("approval prompt cancelled for '%s' -> deny", action_name)
            raise
        except Exception as e:
            logger.error("approval prompt error for '%s': %s -> deny", action_name, e)
            return False

        # Everything past this point runs AFTER asyncio.to_thread already
        # returned — i.e. after the point where wait_for could have cancelled
        # us — so _apply_decision's prefs bookkeeping never runs on a
        # cancelled/timed-out request.
        return self._apply_decision(decision, action_name)


# Register under the APPROVAL_PROVIDER name 'interactive_cli'.
register_approval_provider("interactive_cli", InteractiveCLIApprover)
