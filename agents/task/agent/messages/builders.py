"""Message-builder mixin (roadmap P9; code-motion from message_manager/service.py).

Builders for the typed messages that go into history — state, model-output,
tool-response, and the atomic tool-call pair. Split out so MessageManager's
service.py trends toward orchestration + storage primitives. MessageManager
composes MessageBuildersMixin; call sites unchanged via MRO.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from agents.task.agent.views import AgentBrain, AgentOutput
from modules.llm.messages import AIMessage, ToolMessage


class MessageBuildersMixin:
	"""Typed-message builders for MessageManager."""

	def add_state_message(
		self,
		state: BrowserState,
		result: Optional[List[ActionResult]] = None,
		step_info: Optional[AgentStepInfo] = None,
		use_vision=True,
		previous_brain: Optional['AgentBrain'] = None,
		include_browser_state: bool = True,  # NEW: Toggle browser context
	) -> None:
		"""Add a message with the current state (browser optional).
		
		FIX (Jan 2026): Browser state is now conditional to prevent context
		bleeding into non-browser tasks. Saves ~330 tokens/step.
		
		Args:
			state: BrowserState object (may be minimal for non-browser tasks)
			result: Action results from previous step
			step_info: Current step information
			use_vision: Whether to include screenshot
			previous_brain: Previous step's brain state for memory continuity
			include_browser_state: Whether to include browser URL/tabs/elements
		"""
		# Import locally to avoid circular dependency
		from agents.task.agent.prompts import AgentMessagePrompt
		message = AgentMessagePrompt(
			state=state,
			result=result,
			include_attributes=self.include_attributes,
			max_error_length=self.max_error_length,
			step_info=step_info,
			previous_brain=previous_brain,
			include_browser_state=include_browser_state,  # Pass browser flag
		).get_user_message(use_vision=use_vision)

		# CX-H1: tag the constructed state message so removal can target it by
		# identity, not by guessing its content shape (a plain-string minimal
		# state message and a user's multimodal image turn are otherwise
		# indistinguishable to a shape-based scan).
		message.metadata = {**(getattr(message, "metadata", None) or {}), "state_message": True}

		self._add_message_with_tokens(message)
		

	def add_model_output(self, model_output: AgentOutput, tool_calls: Optional[List[Dict]] = None, llm_content: Optional[str] = None) -> Optional[str]:
		"""Add model output as AI message.

		SINGULAR RESPONSIBILITY: Add AIMessage exactly once after LLM returns.

		FLOW:
		1. LLM returns → add_model_output() called ONCE
		2. AIMessage created with tool_calls
		3. Actions execute
		4. Tool responses added via add_tool_response()

		NO DUPLICATE CHECKS - caller ensures single call.
		NO FALLBACKS - caller provides correct data.

		Args:
			model_output: The agent's output containing actions
			tool_calls: Native tool calls from the LLM response (for native tools)
			llm_content: Text content from LLM (brain state)

		Returns:
			tool_call_id for the ToolMessage after execution
		"""
		if self.use_native_tools and tool_calls:
			# ✅ FIX: Validate content before fallback
			content = llm_content

			if not content or not content.strip():
				self.logger.debug(
					f"[NATIVE_TOOLS] LLM returned tool calls without content (expected in native tool mode). "
					f"Tool calls: {[tc.get('name') for tc in tool_calls[:3]]}{'...' if len(tool_calls) > 3 else ''}"
				)
				content = "Executing actions"
			else:
				self.logger.debug(f"[NATIVE_TOOLS] AI message has content: {content[:100]}...")

			msg = AIMessage(content=content, tool_calls=tool_calls)
			self.logger.debug(f"[NATIVE_TOOLS] Adding AI message with {len(tool_calls)} tool_calls")

			self._add_message_with_tokens(msg, _internal=True)

			# Tool calls should already be registered by Agent
			# Validate they exist in tracker
			if self.tool_call_tracker:
				for tc in tool_calls:
					if not self.tool_call_tracker.has_call(tc.get('id')):
						self.logger.error(
							f"Tool call {tc.get('id')} ({tc.get('name')}) not registered in tracker. "
							f"Agent should register tool calls before calling add_model_output."
						)

			return tool_calls[0].get('id') if tool_calls else None

		# Note: Synthetic tool call creation for non-native tools is handled by Agent using ToolCallBuilder

		# For native tools or if synthetic build failed
		# Add minimal content to AI message to prevent empty steps
		action_summary = "Planning actions"
		if model_output and hasattr(model_output, 'action'):
			try:
				# Render a compact preview of actions without dumping full structure
				action_summary = "Actions planned"
			except Exception:
				pass

		msg = AIMessage(
			content=action_summary,
		)

		self.logger.debug("Adding AI message without tool_calls")
		self._add_message_with_tokens(msg, _internal=True)

		return None

	def _add_output_header(self, content: str) -> str:
		"""Add metadata header to tool outputs for visibility (OPTIMIZATION: Task 2 - Nov 14, 2025)"""
		try:
			import json
			parsed = json.loads(content)

			if isinstance(parsed, list):
				# Make count OBVIOUS
				return f"[TOOL OUTPUT - List with {len(parsed)} items]\n\n{content}"

			elif isinstance(parsed, dict):
				keys = list(parsed.keys())[:5]
				more = f" (+{len(parsed)-5} more)" if len(parsed) > 5 else ""
				return f"[TOOL OUTPUT - Dict with keys: {', '.join(keys)}{more}]\n\n{content}"

		except (json.JSONDecodeError, TypeError):
			pass  # Content is not JSON - return as-is below

		# For large text, show length
		if isinstance(content, str) and len(content) > 100:
			return f"[TOOL OUTPUT - Text, {len(content)} chars]\n\n{content}"

		return content

	def add_tool_response(self, tool_call_id: str, content: str = 'Action completed') -> None:
		"""Add tool response message after action execution.

		If no tool_call_id is provided (native tool call not used), degrade to a
		plain assistant message so providers that don't expect tool role messages
		won't receive invalid sequences.

		P0 FIX: Ensures AI message with tool_calls is present before adding tool response.
		Buffers tool responses if AI message not yet present.
		"""
		if tool_call_id:
			# CRITICAL FIX (P0): Ensure AI message with tool_calls exists before adding tool response
			# This prevents orphan tool responses that cause message sequence errors


			# SIMPLIFIED VALIDATION: Use ToolCallTracker as single source of truth
			if tool_call_id and self.tool_call_tracker:
				if not self.tool_call_tracker.has_call(tool_call_id):
					self.logger.warning(f"Tool response for {tool_call_id} has no matching call in tracker")

			# Track tool response if tracker is available
			if self.tool_call_tracker:
				self.tool_call_tracker.complete_call(
					call_id=tool_call_id,
					result=content
				)

			# MINIMAL FIX (Nov 4, 2025): Remove ALL truncation to stop file-reading loops
			# Problem: ANY truncation causes loops - agent reads file → truncated → reads again
			# Solution: NO TRUNCATION. Let context window handle it (we have 1M tokens available)
			#
			# With gpt-5 (1M context), we can fit:
			# - 200K chars = ~50K tokens = 4.8% of context
			# - 500K chars = ~125K tokens = 12% of context
			# Current usage: ~6.6K tokens = 0.63% of context
			#
			# PLENTY OF ROOM - don't truncate!

			# OPTIMIZATION: Enhance content with metadata header (Task 2 - Nov 14, 2025)
			enhanced_content = self._add_output_header(content)

			# Minimal logging for very large content (>100K chars)
			if len(content) > 100000:
				self.logger.info(
					f"Large tool result: {len(content):,} chars (~{len(content)//4:,} tokens) - "
					f"NO TRUNCATION (context has room)"
				)

			tool_message = ToolMessage(
				content=enhanced_content,
				tool_call_id=tool_call_id,
			)
			self.logger.debug(f"Adding tool message response with tool_call_id: {tool_call_id}")

			# Validate that this is a real response (not a placeholder)
			if not content or content == "Action completed":
				self.logger.warning(f"Tool response may be generic/empty for {tool_call_id}: {content[:100]}")

			# Add the ToolMessage to history with internal flag
			self._add_message_with_tokens(tool_message, _internal=True)
			return

		# Fallback: emit an assistant message with the tool result
		self.logger.debug("No tool_call_id provided – emitting assistant message with tool result")
		ai_msg = AIMessage(content=f"Tool result: {content}")
		self._add_message_with_tokens(ai_msg, _internal=True)

	# === Atomic tool call pair addition ===

	def add_tool_call_pair_atomic(
		self,
		ai_content: str,
		tool_calls: List[Dict[str, Any]],
		tool_responses: List[Tuple[str, str]]
	) -> None:
		"""Add AIMessage with tool_calls and all ToolMessages atomically.

		Ensures OpenAI's requirement: AIMessage with tool_calls must be
		immediately followed by ToolMessages for each tool_call_id.

		Args:
			ai_content: Content for AIMessage
			tool_calls: List of tool call dicts with 'id', 'name', 'args'
			tool_responses: List of (tool_call_id, response_content) tuples

		Raises:
			ValueError: If tool_call_ids don't match response IDs
		"""
		# Validate all tool_calls have responses
		tc_ids = {tc.get('id') for tc in tool_calls if tc.get('id')}
		resp_ids = {resp[0] for resp in tool_responses}

		if tc_ids != resp_ids:
			missing = tc_ids - resp_ids
			extra = resp_ids - tc_ids
			raise ValueError(
				f"Tool call/response mismatch. Missing: {missing}, Extra: {extra}"
			)

		# SECURITY FIX: Protect entire atomic operation with lock
		# This ensures checkpoint, add, and rollback are all atomic
		with self._history_lock:
			# Create checkpoint for rollback
			checkpoint_idx = len(self.history.messages)
			checkpoint_tokens = self.history.total_tokens

			try:
				# Step 1: Add AIMessage with tool_calls
				ai_msg = AIMessage(content=ai_content, tool_calls=tool_calls)
				self._add_message_with_tokens(ai_msg)

				# Step 2: Add ALL ToolMessages immediately after (in order)
				tc_order = {tc.get('id'): idx for idx, tc in enumerate(tool_calls)}
				sorted_responses = sorted(
					tool_responses,
					key=lambda x: tc_order.get(x[0], 999)
				)

				for tc_id, content in sorted_responses:
					tool_msg = ToolMessage(content=content, tool_call_id=tc_id)
					self._add_message_with_tokens(tool_msg)

				self.logger.info(
					f"✅ Atomic add: AIMessage + {len(tool_responses)} ToolMessages"
				)

				# Update tracker if available
				if self.tool_call_tracker:
					for tc_id, content in tool_responses:
						self.tool_call_tracker.complete_call(tc_id, content)

			except Exception as e:
				# Rollback: remove all messages added since checkpoint
				self.logger.error(f"Error in atomic add: {e}, rolling back")

				while len(self.history.messages) > checkpoint_idx:
					removed = self.history.messages.pop()
					# Restore token count
					if hasattr(removed, 'metadata') and hasattr(removed.metadata, 'input_tokens'):
						token_delta = removed.metadata.input_tokens
						self.history.total_tokens -= token_delta

				self.history.total_tokens = checkpoint_tokens
				self.logger.warning("Rolled back atomic addition")
				raise

# _ensure_artifacts_in_context removed - todo system handles task tracking
	
	

