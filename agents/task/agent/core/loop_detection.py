"""Loop-detection + intervention mixin (roadmap P9 decomposition; code-motion from service.py).

Stop-flag checks, multi-signal action-loop detection (action repetition + memory
similarity + goal stagnation), the guidance-injection intervention, and the
identical-call escalation (guidance -> the next identical call is not executed ->
the run ends with ``StopReason.LOOP``). ``Agent`` composes ``LoopDetectionMixin``.
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
    "process_wait", "process_poll", "process_log", "process_list",
})


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

        self._last_result.append(ActionResult(
            error=error_msg,
            include_in_memory=True
        ))

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

    def _loop_guard_check(self, model_output) -> Optional[List[ActionResult]]:
        """Before execution: the error results for a call that must not run again,
        or None to run the step normally. Never raises."""
        try:
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
        three loop signals; ``detect_action_loop`` is the H-MEM multi-signal one and
        ``AgentState.track_action`` the string-similarity one). A repetition is the same
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

    def detect_action_loop(
        self,
        current_brain: Any,
        current_actions: List[Dict],
        lookback_steps: int = 5
    ) -> tuple[bool, Optional[str]]:
        """Detect if agent is repeating the same action pattern.

        PHASE 2 FIX (Nov 4, 2025): Multi-signal loop detection prevents infinite loops
        where agent repeats same actions without progress.

        Detection Signals:
        - Action repetition (same tool called N+ times in recent steps)
        - Memory similarity (semantic overlap >70% in brain state)
        - Goal stagnation (same next_goal for N+ consecutive steps)

        Args:
            current_brain: Current step's brain state
            current_actions: Actions agent wants to take
            lookback_steps: How many recent steps to check (default: 5)

        Returns:
            (is_loop, warning_message) tuple
        """
        if not self.task_context_manager or not self.session_id:
            return False, None

        # Get session data using correct API
        session_data = self.task_context_manager.get_session(self.session_id)
        if not session_data or not session_data.memory:
            return False, None

        memory = session_data.memory
        recent_steps = memory.recent_steps[-lookback_steps:] if hasattr(memory, 'recent_steps') else []

        if len(recent_steps) < 3:
            return False, None  # Need at least 3 steps to detect pattern

        # SIGNAL 1: Action Repetition
        current_action_names = set()
        for action_dict in current_actions:
            if isinstance(action_dict, dict):
                current_action_names.update(action_dict.keys())

        if not current_action_names:
            return False, None

        repeated_action_count = 0
        repeated_step_nums = []
        for step in recent_steps:
            step_summary_lower = step.action_summary.lower()
            if any(action.lower() in step_summary_lower for action in current_action_names):
                repeated_action_count += 1
                repeated_step_nums.append(step.step_number)

        action_repeat_ratio = repeated_action_count / len(recent_steps)

        # SIGNAL 2: Memory Similarity
        similar_memory_count = 0
        similar_memories = []

        if hasattr(current_brain, 'memory') and current_brain.memory:
            import re
            current_keywords = set(re.findall(r'\b\w{4,}\b', current_brain.memory.lower()))

            for step in recent_steps:
                if step.finding:
                    step_keywords = set(re.findall(r'\b\w{4,}\b', step.finding.lower()))
                    if current_keywords and step_keywords:
                        overlap = len(current_keywords & step_keywords)
                        total = len(current_keywords | step_keywords)
                        if total > 0:
                            similarity = overlap / total
                            if similarity > 0.70:  # 70% keyword overlap
                                similar_memory_count += 1
                                similar_memories.append((step.step_number, similarity, step.finding[:60]))

        memory_similarity_ratio = similar_memory_count / len(recent_steps) if recent_steps else 0

        # SIGNAL 3: Goal Stagnation (if available)
        goal_stagnation = False
        if hasattr(current_brain, 'next_goal') and current_brain.next_goal:
            stagnant_goals = 0
            for step in recent_steps:
                if hasattr(step, 'brain_state') and step.brain_state:
                    if hasattr(step.brain_state, 'next_goal'):
                        if step.brain_state.next_goal == current_brain.next_goal:
                            stagnant_goals += 1
            if stagnant_goals >= len(recent_steps) * 0.6:
                goal_stagnation = True

        # DETECTION LOGIC: Multiple signals = higher confidence
        is_loop = False
        warning_message = None

        if action_repeat_ratio >= 0.60:  # Action repeated in 60%+ of steps
            severity = "🔄 LOOP DETECTED"
            if memory_similarity_ratio >= 0.60:  # AND memory is similar
                severity = "🚨 SEVERE LOOP"
                is_loop = True
            elif goal_stagnation:  # AND goal hasn't changed
                severity = "⚠️  POSSIBLE LOOP"
                is_loop = True
            else:
                severity = "⚠️  ACTION REPETITION"
                is_loop = True

            warning_message = (
                f"{severity} at step {self.state.n_steps}:\n\n"
                f"📊 Detection Signals:\n"
                f"  • Action repetition: {action_repeat_ratio:.0%} ({repeated_action_count}/{len(recent_steps)} steps)\n"
                f"  • Memory similarity: {memory_similarity_ratio:.0%} ({similar_memory_count}/{len(recent_steps)} steps)\n"
                f"  • Goal stagnation: {'YES' if goal_stagnation else 'NO'}\n\n"
                f"🔍 Pattern Analysis:\n"
                f"  • Repeated action: {', '.join(current_action_names)}\n"
                f"  • Steps with this action: {repeated_step_nums}\n"
                f"  • Current memory: {current_brain.memory[:100] if hasattr(current_brain, 'memory') else 'N/A'}...\n\n"
                f"💡 Recommended Actions:\n"
                f"  1. Review recent steps to avoid repeating work\n"
                f"  2. Check if data/result already available from previous steps\n"
                f"  3. Try a DIFFERENT approach or tool\n"
                f"  4. Verify your goal hasn't already been achieved\n"
            )

            self.logger.warning(warning_message)

        return is_loop, warning_message
