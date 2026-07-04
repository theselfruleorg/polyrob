"""Safety + lifecycle-control mixin (roadmap P9 decomposition; code-motion from service.py).

The loop-safety (failure/overflow/stall detection, control-flag handling) and
lifecycle (pause/resume/stop/cancel/reset) concern, moved verbatim off the
``Agent`` god-file. ``Agent`` composes ``SafetyLifecycleMixin`` so call sites
(run loop in step.py, and external ``agent.stop()`` / ``agent.reset_for_continuation()``)
are unchanged.
"""
from __future__ import annotations

import asyncio
import time


class SafetyLifecycleMixin:
    """Failure/stall safety + pause/resume/stop/cancel/reset for Agent."""

    def _too_many_failures(self) -> bool:
        """Check if we should stop due to too many failures"""
        if self.state.consecutive_failures >= self.max_failures:
            self.logger.error(f'❌ Stopping due to {self.max_failures} consecutive failures', exc_info=True)
            return True
        return False

    def _check_context_overflow(self) -> bool:
        """Check if context exceeds threshold and trigger emergency prune.

        SIMPLIFIED (Dec 2025): Uses CONTEXT_OVERFLOW_THRESHOLD from config (default 90%).
        Let context grow naturally, only prune when necessary.

        Returns:
            True if pruning was triggered, False otherwise
        """
        if not hasattr(self, 'message_manager') or not self.message_manager:
            return False

        from agents.task.robust_parse_config import RobustParseConfig

        usage_pct = self.message_manager.get_context_usage_percent()
        threshold = RobustParseConfig.CONTEXT_OVERFLOW_THRESHOLD * 100  # Convert to percentage

        if usage_pct >= threshold:
            self.logger.info(f"📦 Context at {usage_pct:.1f}% (threshold: {threshold:.0f}%) - emergency prune")
            self.message_manager.emergency_context_prune()
            return True

        return False

    async def _check_for_stall(self) -> bool:
        """Check if the agent is stalled and not making progress.

        Returns:
            True if agent is stalled and should be stopped
        """
        if not self.stall_timeout_seconds:
            return False

        current_time = time.time()

        # IMPORTANT: Don't report stalls while waiting for LLM response
        # LLM calls can legitimately take 60-200+ seconds for complex tasks
        if self._llm_call_in_progress:
            llm_duration = current_time - (self._llm_call_start_time or current_time)
            # Only log at longer intervals (every 60s) to reduce noise
            if llm_duration > 0 and int(llm_duration) % 60 == 0:
                self.logger.debug(f"⏳ LLM call in progress for {llm_duration:.0f}s - stall check skipped")
            return False

        # Check if we've been inactive for too long (excluding LLM time)
        time_since_activity = current_time - self.state.last_activity_time
        if time_since_activity > self.stall_timeout_seconds:
            self.logger.error(f"Agent stalled: No activity for {time_since_activity:.1f} seconds (limit: {self.stall_timeout_seconds}s)", exc_info=True)
            return True

        # Check if we're stuck on the same step for too long
        if self.state.last_step_start_time:
            time_in_step = current_time - self.state.last_step_start_time
            if time_in_step > self.step_timeout_seconds:
                self.logger.error(f"Agent stalled: Step taking {time_in_step:.1f} seconds (limit: {self.step_timeout_seconds}s)", exc_info=True)
                return True

        # Check if we're not making action progress
        if self.state.total_actions_count == self._last_action_count:
            # Only warn if we're NOT waiting for LLM
            if not self._llm_call_in_progress:
                self.logger.warning(f"No new actions in last {self._stall_check_interval} seconds")
        else:
            # Update last action count and activity time
            self._last_action_count = self.state.total_actions_count
            self.state.last_activity_time = current_time

        return False

    async def _stall_monitor_loop(self) -> None:
        """Background task to monitor for stalls."""
        try:
            while not self.state.stopped:
                await asyncio.sleep(self._stall_check_interval)

                if self.state.stopped:
                    # Don't check for stalls when stopped
                    break

                if await self._check_for_stall():
                    self.logger.error("Stall detected - stopping agent", exc_info=True)
                    self.state.consecutive_failures = self.max_failures  # Force stop
                    self.state.stopped = True
                    break
        except asyncio.CancelledError:
            self.logger.debug("Stall monitor cancelled")
        except Exception as e:
            self.logger.error(f"Error in stall monitor: {e}", exc_info=True)

    async def _handle_control_flags(self) -> bool:
        """Handle stop and done flags. Returns True if execution should stop.

        NOTE: Pause functionality removed (Dec 2025). The blocking wait loop was
        incompatible with continuous chat design where new messages should be processed.

        For user interruption, use stop which cleanly exits. Users can send a new
        message to continue with a different task (auto-resume via COMPLETED → RESUMED).
        """
        # Check state.done to stop execution when agent signals completion
        if self.state.done:
            self.logger.info('✅ Agent execution complete (state.done=True)')
            return True
        if self.state.stopped:
            self.logger.info('⏹️ Agent execution stopped by control flag')
            raise InterruptedError("Agent stopped")

        # Legacy: treat paused as stopped for backwards compatibility
        # The paused flag is deprecated - use stopped instead
        if self.state.paused:
            self.logger.info('⏹️ Agent paused flag set - treating as stop (pause deprecated)')
            self.state.stopped = True  # Normalize to stopped
            raise InterruptedError("Agent stopped (paused flag deprecated)")

        return False

    def pause(self) -> None:
        """DEPRECATED: Pause functionality removed. Now calls stop() instead.

        The PAUSED state was removed because the blocking wait loop was incompatible
        with continuous chat design. Use stop() for user interruption, and send a
        new message to continue with modifications (auto-resume via COMPLETED → RESUMED).
        """
        self.logger.warning('⚠️ pause() is DEPRECATED - redirecting to stop()')
        self.state.stopped = True  # Use stopped instead of paused

    def resume(self) -> None:
        """DEPRECATED: Resume functionality removed. Sessions auto-resume on new message.

        The explicit resume action is no longer needed. When a new message is sent
        to a completed/cancelled session, it automatically resumes via the
        continuous chat flow (COMPLETED → RESUMED → RUNNING).
        """
        self.logger.warning('⚠️ resume() is DEPRECATED - no action taken')
        # No action needed - sessions auto-resume on new message

    async def stop(self) -> None:
        """Stop the agent execution."""
        self.logger.info('⏹️ Stopping agent execution')
        self.state.stopped = True

        # Browser cleanup - release context through orchestrator
        try:
            # Release through orchestrator (delegates to BrowserManager)
            await self.orchestrator.release_browser_context(self.agent_id, close=True)
        except Exception as e:
            self.logger.debug(f"Browser cleanup failed: {e}")

        self.logger.info("Agent stopped")

    def cancel(self) -> None:
        """Cancel the agent execution immediately.

        Sets cancellation flag that will be checked in the main run loop,
        causing execution to stop at the next step boundary.
        """
        self.logger.info('❌ Cancelling agent execution')
        self._cancelled = True

    def reset_for_continuation(self) -> None:
        """Reset agent state for continuous chat continuation.

        Called when reusing an existing agent for a new task phase.
        Resets execution flags while preserving message history and context.

        This is CRITICAL for continuous chat to work properly:
        - Without this reset, _last_result contains is_done=True from previous run
        - The run() loop checks _last_result and exits immediately if is_done=True
        - User's new message would never be processed
        """
        self.logger.info('♻️ Resetting agent state for continuation')

        # Cancel any running stall monitor task from previous run
        # This prevents stale monitors from interfering with new execution
        if hasattr(self, '_stall_check_task') and self._stall_check_task:
            if not self._stall_check_task.done():
                self._stall_check_task.cancel()
                self.logger.debug('Cancelled stale stall monitor task from previous run')
            self._stall_check_task = None

        # Reset last result - prevents immediate exit due to previous is_done=True
        self._last_result = None

        # Reset cancellation flag
        self._cancelled = False

        # Reset execution control flags in AgentState
        self.state.stopped = False
        self.state.done = False
        self.state.paused = False

        # Reset failure counters for fresh execution
        self.state.consecutive_failures = 0

        # Reset loop detection state
        self.state.reset_loop_detection()

        # Update activity time
        self.state.update_activity_time()

        self.logger.info(
            f'✅ Agent reset complete. State: steps={self.state.n_steps}, '
            f'actions={self.state.total_actions_count}, '
            f'history_messages={len(self.message_manager.history.messages)}'
        )
