"""Loop-detection + intervention mixin (roadmap P9 decomposition; code-motion from service.py).

Stop-flag checks, multi-signal action-loop detection (action repetition + memory
similarity + goal stagnation), and the guidance-injection intervention. Moved
verbatim off the ``Agent`` god-file; ``Agent`` composes ``LoopDetectionMixin``
(call sites in the step loop and flow-efficiency tests unchanged via MRO).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from modules.llm.messages import make_control_message, MessageOrigin
from agents.task.agent.views import ActionResult


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
        # Legacy: treat paused as stopped for backwards compatibility
        if self.state.paused:
            self.logger.info('⏹️ Agent paused flag set - treating as stop (pause deprecated)')
            self.state.stopped = True  # Normalize to stopped
            raise InterruptedError("Agent stopped (paused flag deprecated)")
        return False

    def _trigger_loop_intervention(self, reason: str, clear_history: bool = False) -> None:
        """Trigger loop intervention guidance without clearing history.

        Args:
            reason: Human-readable reason for intervention
            clear_history: Deprecated, always False. History is preserved.
        """
        clear_history = False  # History clearing disabled - causes amnesia loops
        self.logger.warning(f"🔄 LOOP DETECTED: {reason}")

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
        if is_validation_error:
            intervention_msg = make_control_message(f"""🚨 TOOL CALL ERROR LOOP DETECTED: {reason}

⚠️ Your tool calls are failing repeatedly with the SAME validation error.
The error message above tells you exactly what's wrong - READ IT CAREFULLY.

🔍 DIAGNOSIS FOR MCP TOOLS:
If calling mcp_execute_tool, the 'arguments' field must be a proper JSON object with the required parameters.
For example, a search tool requires: {{"count": 100, "query": "your search query"}}

🛑 TO FIX THIS:
1. READ the error message - it tells you which parameter is missing
2. INCLUDE all required parameters in your tool call
3. For mcp_execute_tool, ensure 'arguments' contains the nested tool's parameters as a JSON object

Example correct mcp_execute_tool call:
{{
  "server_name": "example_server",
  "tool_name": "search",
  "arguments": {{"count": 100, "query": "crypto tweets"}}
}}

DO NOT just repeat the same broken call. FIX the parameters first.""", MessageOrigin.INTERVENTION)
        else:
            intervention_msg = make_control_message(f"""🚨 CRITICAL LOOP DETECTED: {reason}

⚠️ You have been repeating the same actions without making progress. This is wasting resources.

🛑 YOU MUST DO SOMETHING DIFFERENT NOW:
1. DO NOT read the same files again - you already have their content
2. DO NOT repeat the same action sequence
3. If you need to CREATE something, use filesystem_write_file NOW
4. If you're truly stuck, call the 'done' action with your current progress

📋 Your available actions include:
- filesystem_write_file: CREATE new files (HTML, text, etc.)
- done: Mark task complete with summary

TAKE A DIFFERENT ACTION IMMEDIATELY. Reading the same files again will result in task termination.""", MessageOrigin.INTERVENTION)

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

        # Reset counter and clear action history after intervention
        self._action_repetition_counter = 0
        self._previous_actions.clear()

        # FIX #4: Signal to AgentState for tracking
        if hasattr(self, 'state'):
            self.state.loop_warning_count += 1
            self.state.reset_loop_detection()

        # CRITICAL: History is NEVER cleared - agent retains full context
        self.logger.info(f"✅ Loop intervention complete. Full context preserved, guidance injected.")

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
