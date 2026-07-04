"""Logging / conversation-file output mixin (roadmap P9 decomposition; code-motion from service.py).

The response/context/conversation/tool-output logging concern, moved verbatim off
the ``Agent`` god-file. ``Agent`` composes ``LoggingIOMixin`` so call sites
(``self._log_response``, ``self._log_context_breakdown``, ``self._save_conversation``,
``self._log_tool_outputs`` — invoked from step.py/llm_runner.py) are unchanged.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, List

from modules.llm.messages import BaseMessage, ToolMessage
from agents.task.agent.views import AgentOutput, ActionResult
from agents.task.telemetry.views import AgentRunTelemetryEvent


class LoggingIOMixin:
    """Response/context/conversation/tool-output logging for Agent."""

    def _log_response(self, response: AgentOutput) -> None:
        """Log the model's response"""
        if 'Success' in response.current_state.evaluation_previous_goal:
            status_indicator = "SUCCESS"
        elif 'Failed' in response.current_state.evaluation_previous_goal:
            status_indicator = "FAILED"
        else:
            status_indicator = "UNKNOWN"

        # Log concise brain state summary (memory is most important for continuity)
        self.logger.info(f"Evaluation: [{status_indicator}] {response.current_state.evaluation_previous_goal}")
        self.logger.info(f"Memory: {response.current_state.memory}")

        # Log other fields at debug level to reduce noise
        self.logger.debug(f"Next goal: {response.current_state.next_goal}")
        self.logger.debug(f"Reasoning: {response.current_state.reasoning}")
        if response.current_state.phase:
            self.logger.debug(f"Phase: {response.current_state.phase}")

        if response.action:
            self.logger.info(f"Actions: {len(response.action)} action(s)")
            for i, action in enumerate(response.action):
                try:
                    action_dump = action.model_dump(exclude_unset=True)
                    if not action_dump:
                        # Handle empty action data
                        self.logger.debug(f"  Action {i+1}/{len(response.action)}: Empty action data")
                        continue

                    # Safely get first key
                    action_keys = list(action_dump.keys())
                    if not action_keys:
                        self.logger.debug(f"  Action {i+1}/{len(response.action)}: No keys in action data")
                        continue

                    action_type = action_keys[0]
                    action_params = action_dump[action_type]

                    # Log both the action type and its parameters at INFO level
                    self.logger.info(f"  Action {i+1}/{len(response.action)}: {action_type} - Parameters: {action_params}")
                    # Still keep the detailed JSON in debug for complete data
                    self.logger.debug(f"  Details: {action.model_dump_json(exclude_unset=True)}")
                except Exception as e:
                    # Log error but continue processing other actions
                    self.logger.error(f"Error logging action {i+1}: {str(e)}", exc_info=True)
                    continue

    def _log_context_breakdown(self):
        """Log detailed context usage breakdown.

        FIX (Context Optimization Phase 2): Real-time visibility into context consumption.
        Logs breakdown by category: Foundation, H-MEM, Conversation, Tools
        """
        try:
            # Diagnostic/logging-only call — must peek, not drain, one-shot
            # ephemeral messages (CX-H3). The real provider call in
            # next_action_internal.py keeps the draining default.
            messages = self.message_manager.get_messages_for_llm(consume_ephemeral=False)

            # Count by type
            foundation_tokens = 0
            hmem_tokens = 0
            conversation_tokens = 0
            tool_tokens = 0

            for msg in messages:
                tokens = self.message_manager._count_message_tokens(msg)

                if msg == self.message_manager._system_message:
                    foundation_tokens += tokens
                elif msg == self.message_manager._initial_task_message:
                    foundation_tokens += tokens
                elif hasattr(msg, 'content') and '[Session Memory]' in str(msg.content):
                    hmem_tokens += tokens
                elif isinstance(msg, ToolMessage):
                    tool_tokens += tokens
                else:
                    conversation_tokens += tokens

            total_tokens = sum([foundation_tokens, hmem_tokens, conversation_tokens, tool_tokens])
            usage_pct = (total_tokens / self.message_manager.max_input_tokens) * 100 if self.message_manager.max_input_tokens > 0 else 0

            # Determine emoji
            if usage_pct >= 65:
                emoji = "🔴"
            elif usage_pct >= 55:
                emoji = "🟡"
            else:
                emoji = "🟢"

            self.logger.info(
                f"{emoji} Step {self.state.n_steps} Context: {total_tokens:,}/{self.message_manager.max_input_tokens:,} "
                f"({usage_pct:.1f}%) | "
                f"Foundation: {foundation_tokens:,} | "
                f"H-MEM: {hmem_tokens:,} | "
                f"Conv: {conversation_tokens:,} | "
                f"Tools: {tool_tokens:,}"
            )

            # Warning if approaching compaction
            if usage_pct >= 55:
                self.logger.warning(
                    f"⚠️  Context at {usage_pct:.0f}% - will compact at 65%"
                )

        except Exception as e:
            self.logger.debug(f"Context breakdown failed: {e}")

    def _save_conversation(self, input_messages: list[BaseMessage], response: Any) -> None:
        """Save conversation to disk or telemetry for later retrieval"""
        from agents.task.utils import safe_operation

        # If no save path specified, just return
        if not self.save_conversation_path:
            return

        def save_messages():
            # Ensure path exists
            path = Path(self.save_conversation_path)
            os.makedirs(path.parent, exist_ok=True)

            # Get encoding to use
            encoding = self.save_conversation_path_encoding or 'utf-8'

            with open(path, 'a', encoding=encoding) as f:
                # If file is empty, add headers
                if os.path.getsize(path) == 0:
                    f.write(f'# Task: {self.task}\n')
                    f.write(f'# Created at: {datetime.now().isoformat()}\n')
                    f.write(f'# Model: {self.model_name}\n')
                    f.write(f'# Agent ID: {self.agent_id}\n')
                    f.write(f'# Session ID: {self.session_id}\n\n')

                # Write timestamped step header
                f.write(f'\n## Step {self.state.n_steps} ({datetime.now().isoformat()})\n\n')

                # Write messages
                f.write('### Input Messages\n')
                self._write_messages_to_file(f, input_messages)

                # Write response
                f.write('\n### Response\n')
                self._write_response_to_file(f, response)

                # Write horizontal rule
                f.write('\n---\n')

        safe_operation(
            save_messages,
            self.logger,
            f"Failed to save conversation to {self.save_conversation_path}",
            default_value=None
        )

    def _write_messages_to_file(self, f: Any, messages: list[BaseMessage]) -> None:
        """Write messages to conversation file (sanitised)."""
        import json
        for message in messages:
            f.write(f' {message.__class__.__name__} \n')

            if isinstance(message.content, list):
                for item in message.content:
                    if isinstance(item, dict) and item.get('type') == 'text':
                        clean_txt = self._sanitize_text_for_log(item.get('text', ''))
                        f.write(clean_txt.strip() + '\n')
            elif isinstance(message.content, str):
                try:
                    content = json.loads(message.content)
                    content = self._sanitize_structure_for_log(content)
                    f.write(json.dumps(content, indent=2) + '\n')
                except json.JSONDecodeError:
                    f.write(self._sanitize_text_for_log(message.content.strip()) + '\n')

            f.write('\n')

    def _write_response_to_file(self, f: Any, response: Any) -> None:
        """Write model response to conversation file (sanitised)."""
        import json
        f.write(' RESPONSE\n')
        try:
            resp_dict = json.loads(response.model_dump_json(exclude_unset=True))
            resp_dict = self._sanitize_structure_for_log(resp_dict)
            f.write(json.dumps(resp_dict, indent=2))
        except Exception:
            # Fallback – write raw if sanitisation fails
            f.write(json.dumps(json.loads(response.model_dump_json(exclude_unset=True)), indent=2))

    def _log_agent_run(self) -> None:
        """Log the agent run"""

        self.logger.debug(f'Version: {self.version}, Source: {self.source}')
        self.telemetry_manager.capture_event(
            AgentRunTelemetryEvent(
                agent_id=self.agent_id,
                use_vision=self.use_vision,
                task=self.task,
                model_name=self.model_name,
                chat_model_library=self.chat_model_library,
                version=self.version,
                source=self.source,
            )
        )

    def _log_tool_outputs(self, actions: List[Any], results: List[ActionResult], step_number: int):
        """Log exact tool outputs with item counts (OPTIMIZATION: Task 1 - Nov 14, 2025)"""
        if not self.tool_output_log_path:
            return

        try:
            import json
            from datetime import datetime as _dt

            for idx, (action, result) in enumerate(zip(actions, results)):
                tool_name = action.get('name') if isinstance(action, dict) else getattr(action, 'name', 'unknown')
                arguments = action.get('arguments', {}) if isinstance(action, dict) else {}

                log_entry = {
                    "timestamp": _dt.now().isoformat(),
                    "step": step_number,
                    "tool": tool_name,
                    "arguments": arguments,
                    "result": {
                        "content": result.extracted_content,
                        "error": result.error,
                        "bytes": len(result.extracted_content or "")
                    }
                }

                # Add item count for lists
                if result.extracted_content:
                    try:
                        parsed = json.loads(result.extracted_content)
                        if isinstance(parsed, list):
                            log_entry["result"]["item_count"] = len(parsed)
                            log_entry["result"]["first_item"] = parsed[0] if parsed else None
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        pass  # Content may not be JSON - skip enhanced logging

                # Write to JSONL
                with open(self.tool_output_log_path, 'a') as f:
                    f.write(json.dumps(log_entry) + '\n')

                # Console log with count
                if "item_count" in log_entry["result"]:
                    count = log_entry["result"]["item_count"]
                    requested = arguments.get('count') or arguments.get('limit')

                    if requested:
                        self.logger.info(f"📊 {tool_name}: {count} items (requested: {requested})")
                    else:
                        self.logger.info(f"📊 {tool_name}: {count} items")

        except Exception as e:
            self.logger.error(f"Failed to log tool outputs: {e}")
