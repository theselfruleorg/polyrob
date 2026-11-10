"""Loop-detection + intervention mixin (roadmap P9 decomposition; code-motion from service.py).

Stop-flag checks, the guidance-injection intervention, the identical-call
escalation (guidance -> the next identical call is not executed -> the run ends
with ``StopReason.LOOP``) and the no-progress guard over a whole run.
``Agent`` composes ``LoopDetectionMixin``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from modules.llm.messages import make_control_message, MessageOrigin
from agents.task.agent.views import ActionResult


#: Observe / poll / navigation actions (review 2026-10-29 H2). They return the
#: SAME text while the world changes — '⬇️ Scrolled down', a "Load more" click
#: ('🖱️ Clicked element with index N: <label>'), process_wait on a quiet job
#: ('running (still running after 60s)' + the same log tail) — so "same
#: arguments + same result" is no proof of a loop. A repetition of a step made
#: ONLY of these gets guidance once; it is never blocked and never ends a run.
#: Exact registered names (core/verb_policy_rows.py); verb-level sets stay
#: hand-curated next to their gate (core/tool_capabilities.py is per tool id).
REPEAT_OK_ACTIONS = frozenset({
    "browser_scroll", "browser_scroll_down", "browser_scroll_up", "browser_scroll_to_text",
    "browser_click_element", "browser_send_keys", "browser_go_back_action",
    "browser_refresh_page", "browser_switch_tab", "browser_extract_page_content",
    "browser_click_at", "browser_go_forward", "browser_hover_element",
    "browser_read_element", "browser_wait",
    "process_wait", "process_poll", "process_log", "process_list",
})

#: A run of send-only steps to one recipient (prod 2026-11-06: a job sent the owner
#: 40 reworded lines in 5 minutes and wrote "stopping now" in many of them). The
#: text and the message id change, so the identical-call escalation never saw it.
#: Guidance after REPLY_LOOP_WARN_AT sends in a row; a send after
#: REPLY_LOOP_BLOCK_AT is not run; the attempt after that ends the run.
REPLY_LOOP_WARN_AT = 3
REPLY_LOOP_BLOCK_AT = 4

#: Steps in a row that add nothing new to the run (prod 2026-11-06: 25 reads of 2
#: files at changing offsets and 6 owner messages, no post; earlier runs read one
#: log 66 times). New arguments are not progress when only the window moved. A step
#: is new when a call has a target this run has not used; a send is never new.
#: Guidance at STALE_WARN_AT; at STALE_BLOCK_AT a stale step does not run, and the
#: stale attempt after that ends the run.
STALE_WARN_AT = 8
STALE_BLOCK_AT = 12
#: Arguments that move a window over one target. They do not name a new target.
PAGING_ARGS = frozenset({
    "offset", "limit", "char_offset", "max_chars", "page", "page_size", "cursor",
    "start_line", "end_line", "line", "lines", "max_results", "count",
    "since_id", "until_id", "pagination_token", "next_token",
})
#: A run-ending or waiting call: it is neither progress nor a lack of progress.
_NEUTRAL_ACTIONS = REPEAT_OK_ACTIONS | {"done"}
#: A browser page changes under the same call, so its calls prove nothing here.
_NEUTRAL_PREFIXES = ("browser_", "dapp_browser_")


class LoopDetectionMixin:
    """Stop-flag handling + action-loop detection/intervention for Agent."""

    def _check_if_stopped(self) -> bool:
        """Handle stop flag. Returns True if execution should stop.

        NOTE: Pause functionality removed. This method now only checks for stop.
        When stopped, it raises InterruptedError to break out of the current step.
        Users can send a new message to continue with a different task.
        """
        if self.state.stopped:
            self.logger.info('⏹️ Agent execution stopped by control flag')
            raise InterruptedError("Agent stopped")
        return False

    def _trigger_loop_intervention(self, reason: str, clear_history: bool = False,
                                   observe: bool = False) -> None:
        """Trigger loop intervention guidance without clearing history.

        Args:
            reason: Human-readable reason for intervention
            clear_history: Deprecated, always False. History is preserved.
            observe: the repetition is an observe/poll/navigation step
                (``REPEAT_OK_ACTIONS``): a short note only — no error result,
                no "will not run" warning, because nothing is blocked.
        """
        clear_history = False  # History clearing disabled - causes amnesia loops
        self.logger.warning(f"🔄 LOOP DETECTED: {reason}")
        if observe:
            self.message_manager.add_message(make_control_message(
                f"Note: {reason}. If you scroll, page or wait for something that does "
                "not change, change the approach (a longer wait, another element, a "
                "different source) or report what you have. This call is not blocked.",
                MessageOrigin.INTERVENTION))
            return

        # Check if this looks like a validation/parameter error loop
        is_validation_error = any(x in reason.lower() for x in [
            'validation', 'parameter', 'missing required', 'invalid argument',
            'schema', 'type error', 'arguments'
        ])

        # Also check if recent results have validation errors
        if hasattr(self, '_last_result') and self._last_result:
            for result in self._last_result[-3:]:
                if hasattr(result, 'error') and result.error:
                    error_lower = str(result.error).lower()
                    if any(x in error_lower for x in ['missing required parameter', 'validation failed', 'invalid']):
                        is_validation_error = True
                        break

        # History clearing disabled - preserves context
        # if clear_history and hasattr(self, 'message_manager'):
        #     self.message_manager.clear_history_keep_system(keep_last_n=2)

        # Build intervention message based on error type.
        # Chat-schema: tag as INTERVENTION control content (origin + <system-directive>
        # envelope) so it is not indistinguishable from a genuine user turn.
        # F12: name only REGISTERED actions — a directive naming an absent tool
        # (mcp_execute_tool / filesystem_write_file on a rig without them) sends
        # the model to call something that does not exist.
        try:
            _ctrl = getattr(self, "controller", None)
            _names = set(_ctrl.get_action_names()) if _ctrl else set()
        except Exception:
            _names = set()
        _esc = getattr(self, '_loop_escalation', None) or {}
        _next = (" The next identical call will NOT run, and one more after that ends "
                 "this run." if _esc.get('stage') == 'warned' else "")
        if is_validation_error:
            _mcp = ""
            if "mcp_execute_tool" in _names:
                _mcp = """

🔍 FOR MCP TOOLS: when you call mcp_execute_tool, the 'arguments' field must be a
JSON object that holds the nested tool's required parameters, for example:
{"server_name": "example_server", "tool_name": "search",
 "arguments": {"count": 100, "query": "crypto tweets"}}"""
            intervention_msg = make_control_message(f"""🚨 TOOL CALL ERROR LOOP DETECTED: {reason}

⚠️ Your tool calls are failing repeatedly with the SAME validation error.
The error message above tells you exactly what's wrong - READ IT CAREFULLY.

🛑 TO FIX THIS:
1. READ the error message - it tells you which parameter is missing
2. INCLUDE all required parameters in your tool call{_mcp}

DO NOT just repeat the same broken call. FIX the parameters first.{_next}""", MessageOrigin.INTERVENTION)
        else:
            _create = ("3. If you need to CREATE something, use filesystem_write_file NOW\n"
                       if "filesystem_write_file" in _names else "")
            _stuck_n = "4" if _create else "3"
            intervention_msg = make_control_message(f"""🚨 CRITICAL LOOP DETECTED: {reason}

⚠️ You have been repeating the same actions without making progress. This is wasting resources.

🛑 YOU MUST DO SOMETHING DIFFERENT NOW:
1. DO NOT read the same files again - you already have their content
2. DO NOT repeat the same action sequence
{_create}{_stuck_n}. If you're truly stuck, call the 'done' action with your current progress

TAKE A DIFFERENT ACTION IMMEDIATELY.{_next}""", MessageOrigin.INTERVENTION)

        self.message_manager.add_message(intervention_msg)

        # Add error to force reconsideration
        if not hasattr(self, '_last_result') or not self._last_result:
            self._last_result = []

        error_msg = f"LOOP DETECTED: {reason}."
        if clear_history:
            error_msg += " Context cleared."
        error_msg += " You MUST fix the issue - DO NOT repeat the same broken call."

        # A fresh list: the step's own list may be marked as already sent in
        # ToolMessages (F14, by identity), and an append to it would hide this error.
        self._last_result = [*self._last_result, ActionResult(
            error=error_msg,
            include_in_memory=True
        )]

        # The identical-call counter is NOT reset here: an intervention is the first
        # rung of the escalation (_loop_guard_check blocks the next identical call and
        # ends the run on the one after). Resetting it made the warning fire every few
        # steps forever (prod 2026-10-28: 139 identical reads, 16 warnings in one turn).

        # FIX #4: Signal to AgentState for tracking
        if hasattr(self, 'state'):
            self.state.loop_warning_count += 1
            self.state.reset_loop_detection()

        # CRITICAL: History is NEVER cleared - agent retains full context
        self.logger.info(f"✅ Loop intervention complete. Full context preserved, guidance injected.")

    # --- identical-call escalation (prod 2026-10-26..28) ----------------------
    #
    # One rung per further identical call, keyed on the step's arguments AND its
    # result: (1) guidance at the configured repetition threshold, (2) the next
    # identical call is NOT executed — it gets an error that names the repetition
    # and the earlier result, (3) the one after ends the run with StopReason.LOOP.
    # A call whose result changes (polling) never counts; any different call
    # clears the escalation, since the world may have changed. A step made only
    # of REPEAT_OK_ACTIONS (scroll, a "Load more" click, process_wait) gets the
    # guidance rung only. A step whose every result is an error is not counted
    # here: the 093 retry wall (tools/controller/execution.py) owns identical
    # failures. An identical A-B-A-B cycle escalates like one repeated call.

    @staticmethod
    def _step_dump(model_output) -> list:
        dump = []
        for a in (getattr(model_output, 'action', None) or []):
            try:
                dump.append(a.model_dump(mode="json", exclude_unset=True, by_alias=True))
            except Exception:
                dump.append(repr(a))
        return dump

    @classmethod
    def _step_action_hash(cls, model_output) -> Optional[str]:
        dump = cls._step_dump(model_output)
        if not dump:
            return None
        return hashlib.md5(json.dumps(dump, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _step_result_text(results) -> str:
        parts = []
        for r in (results or []):
            err = getattr(r, 'error', None)
            parts.append(f"ERROR: {err}" if err else str(getattr(r, 'extracted_content', '') or ''))
        return " | ".join(p for p in parts if p)

    @staticmethod
    def _step_tool_names(dump: list) -> str:
        names = []
        for d in dump:
            if isinstance(d, dict):
                names.extend(str(k) for k in d.keys() if k != 'interacted_element')
        return ", ".join(dict.fromkeys(names)) or "the same call"

    @staticmethod
    def _esc_hashes(esc: Dict[str, Any]) -> frozenset:
        """The action hashes an escalation covers: one call, or the two calls of
        an A-B-A-B cycle."""
        return frozenset(esc.get('hashes') or ()) | {esc.get('hash')}

    @staticmethod
    def _is_observe_step(dump: list) -> bool:
        names = [str(k) for d in dump if isinstance(d, dict)
                 for k in d.keys() if k != 'interacted_element']
        return bool(names) and all(n in REPEAT_OK_ACTIONS for n in names)

    def _reset_loop_guard(self) -> None:
        """A new run starts with a clean escalation (the run loop calls this)."""
        self._loop_escalation = None
        self._loop_observe_hashes = set()
        self._loop_blocked_step = False
        self._loop_stop_text = None
        self._reply_streak = None
        self._seen_targets = set()
        self._stale_steps = 0
        self._stale_blocked = False
        self._errored_tools = set()
        self._tool_claim_refusals = 0
        # owner_answer: one corrective step per owner message, so a new run or a
        # new owner batch may earn it again, even with the same words ("status?").
        self._owner_answer_nudged = None
        self._last_step_key = None
        self._action_repetition_counter = 0
        # 098 AG-04: the A-B-A-B window is per run too (a cycle never spans turns).
        previous = getattr(self, '_previous_actions', None)
        if previous is not None:
            previous.clear()

    def _take_loop_stop(self) -> Optional[str]:
        """The loop-stop reason once (the run loop ends the run on it), else None."""
        text = getattr(self, '_loop_stop_text', None)
        self._loop_stop_text = None
        return text

    @staticmethod
    def _reply_target(dump: list) -> Optional[str]:
        """The one recipient of a step made only of sends, else None. A `message`
        read (posts, delete) is not a send, and any other action is work."""
        targets = set()
        for d in dump:
            if not isinstance(d, dict):
                return None
            names = [k for k in d if k != 'interacted_element']
            if len(names) != 1:
                return None
            params = d[names[0]] if isinstance(d[names[0]], dict) else {}
            if names[0] == 'send_message':
                targets.add('owner')
            elif names[0] == 'message' and params.get('action') in (None, 'send'):
                target = str(params.get('target') or 'owner')
                targets.add('owner' if target == 'owner'
                            else f"{params.get('surface') or ''}:{target}")
            else:
                return None
        return targets.pop() if len(targets) == 1 else None

    def _reply_loop_check(self, model_output) -> Optional[List[ActionResult]]:
        """The refusal for one more send in a run of sends to one recipient, or None."""
        streak = getattr(self, '_reply_streak', None)
        if not streak or streak['count'] < REPLY_LOOP_BLOCK_AT:
            return None
        if self._reply_target(self._step_dump(model_output)) != streak['target']:
            return None
        self._loop_blocked_step = True
        n = streak['count']
        if not streak.get('blocked'):
            streak['blocked'] = True
            self.logger.warning(f"🛑 LOOP: send not run, {n} sends to one recipient with no new work")
            error = (
                f"MESSAGE NOT SENT: you sent {n} messages in a row to this recipient and "
                "did nothing new. Those messages were delivered. Call done now, or do the "
                "remaining work with another tool. One more send ends this run.")
        else:
            from agents.task.runtime.stop_reason import StopReason, set_stop
            self.logger.error("🛑 LOOP: send repeated after a block — ending the run")
            self._loop_stop_text = f"{n} sends to one recipient with no new work, and one more after a block"
            set_stop(self, StopReason.LOOP)
            error = "RUN ENDED: you tried to send again after the send was blocked."
        n_actions = max(1, len(getattr(model_output, 'action', None) or []))
        return [ActionResult(error=error, include_in_memory=True) for _ in range(n_actions)]

    def _record_reply_streak(self, action_dump: list, stale: Optional[bool] = False) -> None:
        """Count a send-only step. New work or another recipient restarts the count;
        a stale step between two sends does not (prod 2026-11-06: a re-read between
        each of 6 owner messages hid them from this count)."""
        target = self._reply_target(action_dump)
        if target is None:
            if not stale:
                self._reply_streak = None
            return
        streak = getattr(self, '_reply_streak', None)
        if not streak or streak['target'] != target:
            streak = self._reply_streak = {'target': target, 'count': 0}
        streak['count'] += 1
        if streak['count'] == REPLY_LOOP_WARN_AT:
            self.logger.warning(f"🔄 LOOP DETECTED: {streak['count']} sends in a row to one recipient")
            self.message_manager.add_message(make_control_message(
                f"You sent {streak['count']} messages to this recipient and did nothing "
                "new. They were delivered: do not restate them, and do not announce "
                "that you stop. Call done now, or do the remaining work with another tool. "
                "Sends in a row are refused soon, and then the run ends.",
                MessageOrigin.INTERVENTION))

    # --- no-progress guard (prod 2026-11-06) -----------------------------------
    #
    # The identical-call escalation needs the same arguments and the same result.
    # A walk over one file changes the offset each time, so it never counts. This
    # guard keys on the target: the tool plus its arguments without PAGING_ARGS.

    @staticmethod
    def _step_targets(dump: list) -> Optional[List[tuple]]:
        """``(is_send, target)`` per call of a step, or None for a step made only of
        neutral calls."""
        out: List[tuple] = []
        for d in dump:
            if not isinstance(d, dict):
                return [(False, repr(d))]
            for name, params in d.items():
                if (name == 'interacted_element' or name in _NEUTRAL_ACTIONS
                        or name.startswith(_NEUTRAL_PREFIXES)):
                    continue
                params = params if isinstance(params, dict) else {}
                is_send = name == 'send_message' or (
                    name == 'message' and params.get('action') in (None, 'send'))
                identity = {k: v for k, v in params.items() if k not in PAGING_ARGS}
                out.append((is_send, f"{name}:{json.dumps(identity, sort_keys=True, default=str)}"))
        return out or None

    def _step_is_stale(self, dump: list) -> Optional[bool]:
        """True when the step adds nothing new, None for a neutral step. Reads state only."""
        targets = self._step_targets(dump)
        if targets is None:
            return None
        seen = getattr(self, '_seen_targets', None) or set()
        return all(is_send or target in seen for is_send, target in targets)

    def _record_progress(self, dump: list) -> Optional[bool]:
        """Count a stale step, or restart the count on a new target. Returns the
        verdict of :meth:`_step_is_stale`."""
        stale = self._step_is_stale(dump)
        if stale is None:
            return None
        if getattr(self, '_seen_targets', None) is None:
            self._seen_targets = set()
        self._seen_targets.update(t for is_send, t in self._step_targets(dump) if not is_send)
        if not stale:
            self._stale_steps = 0
            self._stale_blocked = False
            return False
        self._stale_steps = getattr(self, '_stale_steps', 0) + 1
        if self._stale_steps == STALE_WARN_AT:
            self.logger.warning(f"🔄 LOOP DETECTED: {self._stale_steps} steps with nothing new")
            self.message_manager.add_message(make_control_message(
                f"Your last {self._stale_steps} steps added nothing new to this run. A new "
                "offset on a file you already read, or one more message, is not new work. "
                "Use what you already have: do the next write or post now, or call done "
                "with the result. Soon these calls are refused, and then the run ends.",
                MessageOrigin.INTERVENTION))
        return True

    def _stale_check(self, model_output) -> Optional[List[ActionResult]]:
        """The refusal for one more stale step after STALE_BLOCK_AT, or None."""
        n = getattr(self, '_stale_steps', 0)
        if n < STALE_BLOCK_AT or not self._step_is_stale(self._step_dump(model_output)):
            return None
        self._loop_blocked_step = True
        if not getattr(self, '_stale_blocked', False):
            self._stale_blocked = True
            self.logger.warning(f"🛑 LOOP: step not run, {n} steps with nothing new")
            error = (f"STEP NOT RUN: your last {n} steps added nothing new to this run. Use "
                     "what you already have: do the next write or post, use a tool or a "
                     "target you have not used, or call done. One more such step ends this run.")
        else:
            from agents.task.runtime.stop_reason import StopReason, set_stop
            self.logger.error("🛑 LOOP: stale step repeated after a block — ending the run")
            self._loop_stop_text = f"{n} steps with nothing new, and one more after a block"
            set_stop(self, StopReason.LOOP)
            error = "RUN ENDED: you tried another step with nothing new after a refusal."
        n_actions = max(1, len(getattr(model_output, 'action', None) or []))
        return [ActionResult(error=error, include_in_memory=True) for _ in range(n_actions)]

    def _tool_claim_check(self, model_output) -> Optional[List[ActionResult]]:
        """The refusal for a text that says a registered tool does not exist, or None."""
        from agents.task.runtime.tool_claims import MAX_REFUSALS, refusal_for_step
        if (getattr(self, '_is_sub_agent', False)
                or getattr(self, '_tool_claim_refusals', 0) >= MAX_REFUSALS):
            return None
        controller = getattr(self, 'controller', None)
        names = set(controller.get_action_names()) if controller else set()
        error = refusal_for_step(self._step_dump(model_output), names,
                                 getattr(self, '_errored_tools', None) or set())
        if error is None:
            return None
        self._tool_claim_refusals = getattr(self, '_tool_claim_refusals', 0) + 1
        self._loop_blocked_step = True
        self.logger.warning("🛑 text refused: it says a registered tool does not exist")
        n_actions = max(1, len(getattr(model_output, 'action', None) or []))
        return [ActionResult(error=error, include_in_memory=True) for _ in range(n_actions)]

    def _track_tool_errors(self, action_dump: list, results: list) -> None:
        """Remember the tools whose last call failed: a claim about them can be true."""
        errored = getattr(self, '_errored_tools', None)
        if errored is None:
            errored = self._errored_tools = set()
        for dumped, result in zip(action_dump, results):
            if isinstance(dumped, dict):
                for name in dumped:
                    (errored.add if getattr(result, 'error', None) else errored.discard)(name)

    def _loop_guard_check(self, model_output) -> Optional[List[ActionResult]]:
        """Before execution: the error results for a call that must not run again,
        or None to run the step normally. Never raises."""
        try:
            refused = (self._tool_claim_check(model_output)
                       or self._reply_loop_check(model_output)
                       or self._stale_check(model_output))
            if refused is not None:
                return refused
            esc = getattr(self, '_loop_escalation', None)
            if not esc:
                return None
            if esc.get('stage') == 'observe':
                return None
            action_hash = self._step_action_hash(model_output)
            if action_hash is None or action_hash not in self._esc_hashes(esc):
                return None
            self._loop_blocked_step = True
            tools = esc.get('tools') or "the same call"
            earlier = (esc.get('result') or "(no result text)")[:600]
            n = esc.get('count') or 0
            if esc.get('stage') == 'warned':
                esc['stage'] = 'blocked'
                self.logger.warning(f"🛑 LOOP: identical call to {tools} not executed (repeated {n} times)")
                error = (
                    f"REPEATED CALL NOT RUN: {tools} with these exact arguments already ran "
                    f"{n} times in a row and returned the same result each time, so it was "
                    f"not run again. The earlier result: {earlier}\n"
                    "Use that result, change the arguments, use a different tool, or call "
                    "done with what you have. One more identical call ends this run.")
            else:
                from agents.task.runtime.stop_reason import StopReason, set_stop
                self.logger.error(f"🛑 LOOP: identical call to {tools} repeated after a block — ending the run")
                self._loop_stop_text = (
                    f"{tools} repeated with the same arguments and the same result "
                    f"after a warning and a block")
                set_stop(self, StopReason.LOOP)
                error = (
                    f"RUN ENDED: {tools} was called again with the same arguments after "
                    f"it was blocked. The earlier result: {earlier}")
            n_actions = max(1, len(getattr(model_output, 'action', None) or []))
            return [ActionResult(error=error, include_in_memory=True) for _ in range(n_actions)]
        except Exception:
            self.logger.debug("loop guard check failed (fail-open)", exc_info=True)
            return None

    def _record_action_for_loop_detection(self, model_output) -> None:
        """Hash-based action-repetition + alternating-pattern detector (the second of the
        loop signals; ``AgentState.track_action`` is the string-similarity one and
        ``_record_progress`` the no-progress one). A repetition is the same
        arguments AND the same result as the previous step; it feeds the escalation
        above. ``_previous_actions`` / ``_action_repetition_counter`` /
        ``_max_allowed_repetitions`` are set in construction.
        """
        if not (model_output and hasattr(model_output, 'action') and model_output.action):
            return
        action_dump = self._step_dump(model_output)
        action_hash = self._step_action_hash(model_output)

        # The NAMES too: `_has_active_browser_usage` needs them, and the hashes
        # above cannot answer "was that a browser action?".
        names = getattr(self, '_recent_action_names', None)
        if names is not None:
            for dumped in action_dump:
                if isinstance(dumped, dict):
                    names.extend(str(k) for k in dumped.keys())

        # A step the guard refused did not run: it is neither a new repetition nor
        # progress, so the escalation state stays as it is.
        if getattr(self, '_loop_blocked_step', False):
            self._loop_blocked_step = False
            return

        # Before the error return below: a send refused as a duplicate is still a
        # send in the run (prod 2026-11-06: 7 of the 40 were refused).
        stale = self._record_progress(action_dump)
        self._record_reply_streak(action_dump, stale)
        self._track_tool_errors(action_dump, list(getattr(self, '_last_result', None) or []))

        # A step whose every result is an error is the 093 retry wall's to count
        # (tools/controller/execution.py: the operation key, max retries, then two
        # wall-only steps end the run). Counting it here too sent one identical
        # FAILING call toward two different stops (LOOP and FAILURES); the wall
        # also catches a failure whose text changes. It breaks a same-result run.
        last = list(getattr(self, '_last_result', None) or [])
        if last and all(getattr(r, 'error', None) for r in last):
            self._action_repetition_counter = 0
            self._last_step_key = None
            self._previous_actions.clear()
            return

        result_text = self._step_result_text(last)
        step_key = f"{action_hash}:{hashlib.md5(result_text.encode()).hexdigest()}"
        observe = self._is_observe_step(action_dump)
        if observe:
            hashes = getattr(self, '_loop_observe_hashes', None)
            if hashes is None:
                hashes = self._loop_observe_hashes = set()
            hashes.add(action_hash)
        esc = getattr(self, '_loop_escalation', None)
        if esc and action_hash not in self._esc_hashes(esc):
            self._loop_escalation = esc = None

        if step_key == getattr(self, '_last_step_key', None):
            self._action_repetition_counter += 1
            if self._action_repetition_counter >= 3:
                self.logger.warning(f"Same action repeated {self._action_repetition_counter} times")
            if self._action_repetition_counter >= self._max_allowed_repetitions and not esc:
                tools = self._step_tool_names(action_dump)
                self._loop_escalation = {
                    'hash': action_hash, 'stage': 'observe' if observe else 'warned',
                    'tools': tools, 'count': self._action_repetition_counter + 1,
                    'result': result_text}
                self._trigger_loop_intervention(
                    f"{tools} repeated {self._action_repetition_counter + 1} times in a row "
                    f"with the same arguments and the same result",
                    **({'observe': True} if observe else {}))
            elif esc:
                esc['count'] = self._action_repetition_counter + 1
        else:
            self._action_repetition_counter = 0
        self._last_step_key = step_key

        # Pattern history keys on the result too: a re-check whose answer changed
        # is not a cycle.
        self._previous_actions.append(step_key)
        if len(self._previous_actions) >= 4:
            prev = list(self._previous_actions)
            pattern = None
            cycle = None
            if prev[-1] == prev[-3] and prev[-2] == prev[-4] and prev[-1] != prev[-2]:
                pattern = "Alternating A-B-A-B pattern detected"
                cycle = {prev[-1].split(':', 1)[0], prev[-2].split(':', 1)[0]}
            elif len(prev) >= 6 and prev[-3:] == prev[-6:-3] and len(set(prev[-3:])) > 1:
                pattern = "Repeating 3-action pattern detected"
            if pattern:
                self.logger.warning(f"🔄 {pattern} - INTERVENING NOW")
                observed = getattr(self, '_loop_observe_hashes', None) or set()
                cycle_observe = bool(cycle) and cycle <= observed
                # L4: an identical two-call cycle (same arguments, same results)
                # escalates like one repeated call: the next call of the cycle is
                # not run, the one after ends the run. An observe-only cycle and
                # the 3-call pattern stay guidance.
                if cycle and not cycle_observe and not getattr(self, '_loop_escalation', None):
                    self._loop_escalation = {
                        'hash': action_hash, 'hashes': sorted(cycle), 'stage': 'warned',
                        'tools': "the same two calls in turn", 'count': 2,
                        'result': result_text}
                self._trigger_loop_intervention(
                    pattern, **({'observe': True} if cycle_observe else {}))
                # Start the pattern window afresh so it does not fire again on
                # every following step.
                self._previous_actions.clear()
