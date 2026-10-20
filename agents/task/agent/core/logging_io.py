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

from modules.llm.messages import BaseMessage
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
        """Log the per-step context reading — from the ONE gauge (F19).

        This used to walk the assembled message list, re-count every message and
        divide by ``max_input_tokens`` itself: a third total that disagreed with
        `/context`, with ``get_actual_token_count`` and with the status bar,
        because it saw neither the tool schemas nor the ephemerals and it
        classified H-MEM by a substring. It now reads
        ``MessageManager.context_usage()`` and prints the provenance, so the log
        line, the compaction ladder and the owner's `/context` are the same
        number.
        """
        try:
            usage = self.message_manager.context_usage()
            total_tokens = usage["total_tokens"]
            limit = usage["limit"]
            usage_pct = usage["pct"]
            slots = usage["slots"]
            # ≈ when nothing has been measured yet; the provider's own
            # prompt_tokens once a call has completed.
            mark = "" if usage["source"] == "provider_usage" else "≈"

            # Determine emoji — the bands are the ONE compaction ladder
            # (agents/task/constants.py), never a literal here.
            from agents.task.constants import COMPACTION_LLM_PCT, COMPACTION_WARN_PCT
            if usage_pct >= COMPACTION_LLM_PCT:
                emoji = "🔴"
            elif usage_pct >= COMPACTION_WARN_PCT:
                emoji = "🟡"
            else:
                emoji = "🟢"

            line = (
                f"{emoji} Step {self.state.n_steps} Context: {mark}{total_tokens:,}/{limit:,} "
                f"({usage_pct:.1f}%, {usage['source']}) | "
                f"Foundation: {slots['foundation']:,} | "
                f"H-MEM: {slots['hmem']:,} | "
                f"Conv: {slots['conversation']:,} | "
                f"Ephemeral: {slots['ephemeral']:,} | "
                f"Schemas: {slots['tool_schemas']:,}"
            )
            last_call = usage.get("last_call") or {}
            if last_call.get("prompt_tokens"):
                line += (
                    f" | last call: {last_call['prompt_tokens']:,} prompt, "
                    f"{last_call.get('cached_tokens', 0):,} cached, "
                    f"{last_call.get('cache_creation_tokens', 0):,} written"
                )
            self.logger.info(line)

            # Warning if approaching compaction (same ladder).
            if usage_pct >= COMPACTION_WARN_PCT:
                self.logger.warning(
                    f"⚠️  Context at {usage_pct:.0f}% - will compact at {COMPACTION_LLM_PCT}%"
                )

        except Exception as e:
            self.logger.debug(f"Context breakdown failed: {e}")

    def _emit_prompt_manifest(self, slots: dict | None = None) -> None:
        """2026-10-14 review D-10: ONE ``prompt_manifest`` row per session —
        the slot sizes of the first step's prompt, the skills active, the
        regime and model, and the size of the ``<previous-runs>`` block the task
        carries. Sizes only, never text. Fail-open.

        081 WS-0.2: called from ``_call_llm`` once the first call returned
        (step 1 only), so the row sees what the request carried: the tool-schema
        cost (set inside ``get_next_action``), the step-1 blocks (recall,
        ``<live-health>``, ``<operations>``, digests, bridges — consumed into the
        pending buffer), one size per ``FOUNDATION_LAYERS`` row, and the
        provider's own ``prompt_tokens`` when the call reported usage."""
        try:
            import re as _re
            from core.prompt_manifest import emit_prompt_manifest
            extra = {}
            if slots is None:
                if getattr(getattr(self, "state", None), "n_steps", 1) != 1:
                    return
                slots, extra = self._prompt_manifest_measure()
            try:
                from agents.task.session_class import session_regime
                regime = session_regime(self)
            except Exception:
                regime = ""
            act = getattr(getattr(self, "controller", None), "_activated_skills", None)
            skills = [str(x) for x in act if x and "#" not in str(x)] \
                if isinstance(act, (set, frozenset, list, tuple)) else None
            task = str(getattr(self, "task", "") or "")
            m = _re.search(r"<previous-runs\b.*?</previous-runs>", task, _re.S)
            extra["precheck"] = "<precheck" in task
            emit_prompt_manifest(
                session_id=str(getattr(self, "session_id", "") or ""),
                user_id=str(getattr(self, "user_id", "") or ""),
                regime=regime,
                model=str(getattr(getattr(self, "llm", None), "model_name", "") or ""),
                slots=dict(slots or {}), skills=skills,
                previous_runs_chars=len(m.group(0)) if m else 0,
                extra=extra)
        except Exception as e:
            self.logger.debug(f"prompt manifest skipped: {e}")

    def _prompt_manifest_measure(self) -> tuple:
        """(slots, extra) for the first-call manifest row (081 WS-0.2).

        ``slots`` is the ONE gauge (``context_usage()``) with the ephemeral slot
        recounted over the queued AND the consumed-for-this-call blocks (the
        gauge counts only the queued ones, and the call consumed them).
        ``extra`` carries ``layers`` (tokens per ``FOUNDATION_LAYERS`` key,
        empty layers left out), ``ephemeral`` (tokens per origin) and the
        provider's ``prompt_tokens``/``cached_tokens`` when the call reported
        usage."""
        from agents.task.agent.messages.foundation_layers import FOUNDATION_LAYERS
        mm = self.message_manager
        usage = mm.context_usage()
        slots = dict(usage.get("slots") or {})
        blocks = list(getattr(mm, "_ephemeral_messages", None) or []) \
            + list(getattr(mm, "_ephemeral_pending", None) or [])
        by_origin: dict = {}
        for msg in blocks:
            key = str(getattr(msg, "origin", None) or "untagged")
            by_origin[key] = by_origin.get(key, 0) + mm.estimate_tokens([msg])
        slots["ephemeral"] = sum(by_origin.values())
        layers = {layer.key: int(getattr(mm, layer.tokens_attr, 0) or 0)
                  for layer in FOUNDATION_LAYERS}
        extra = {"layers": {k: v for k, v in layers.items() if v},
                 "ephemeral": by_origin}
        last = usage.get("last_call") or {}
        if last.get("prompt_tokens"):
            extra["prompt_tokens"] = int(last["prompt_tokens"])
            extra["cached_tokens"] = int(last.get("cached_tokens", 0) or 0)
        return slots, extra

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
