"""User-guidance injection mixin (roadmap P9; code-motion from message_manager/service.py).

inject_user_guidance is policy (how user/continuation guidance enters the message
context), not raw storage — §1.3(vi). Split into its own mixin so MessageManager's
service.py shrinks toward storage/retrieval. MessageManager composes GuidanceMixin;
callers (agent service.py, step.py) use message_manager.inject_user_guidance unchanged.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from modules.llm.messages import HumanMessage, MessageOrigin, SystemMessage

# Forged (non-human) intake kinds → their true origin. Anything else — comment,
# continuation, correction, approval — is a genuine human turn (origin USER).
# A batch is only marked forged when EVERY drained message is forged: a real
# user message must never be demoted by a co-drained wake turn.
_FORGED_KIND_ORIGINS = {
	"self_wake": MessageOrigin.SELF_WAKE,
	"delegation_result": MessageOrigin.SYSTEM_NOTE,
}


def _elide_middle(text: str, keep_head: int, keep_tail: int) -> str:
	"""Head+tail middle-elision (P0-1).

	Keeps the first ``keep_head`` and last ``keep_tail`` characters with an explicit
	marker between them. Unlike a plain ``text[:n]`` cut this preserves BOTH the
	opening context and the trailing content — critical for pre-wrapped forged bodies
	whose closing ``</untrusted_tool_result>`` / ``</delegation-result>`` delimiter
	lives at the end (a head-only cut would leave the tag open).
	"""
	limit = keep_head + keep_tail
	if not isinstance(text, str) or len(text) <= limit:
		return text
	elided = len(text) - limit
	return f"{text[:keep_head]}\n[... {elided} chars elided ...]\n{text[-keep_tail:]}"


class GuidanceMixin:
	"""User/continuation guidance injection for MessageManager."""

	def inject_user_guidance(self, messages: List[Dict[str, Any]], session_context: Optional[Dict] = None) -> None:
		"""Inject user guidance into message context for continuous conversation.

		Implements Anthropic's "minimal, high-signal tokens" principle for continuous chat.
		Reference: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

		Args:
			messages: List of user message dictionaries with text, kind, metadata
			session_context: Optional session context with fields:
				- continuation: bool (True if user sent message to ongoing session)
		"""
		if not messages:
			return

		# Use RobustParseConfig for safety
		from agents.task.robust_parse_config import RobustParseConfig
		config = RobustParseConfig()

		# Extract message texts. P0-1: forged (self-wake / delegation-result) bodies are
		# pre-wrapped and bounded at their source — truncating them here would strip the
		# closing delimiter and leave an open <untrusted_tool_result> tag, and the old
		# 500-char cut delivered ZERO payload (preamble+boilerplate alone exceed 500).
		# Genuine messages get head+tail elision so a long paste keeps both its ask and
		# its trailing detail instead of a hard [:500] cut.
		drained = messages[:config.MAX_USER_MESSAGES_PER_STEP]
		message_texts = []
		for msg in drained:
			text = msg.get('text', '')
			kind = msg.get('kind', 'comment')
			if kind in _FORGED_KIND_ORIGINS:
				# Bounded, delimiter-preserving ceiling only (no per-message cut).
				text = _elide_middle(
					text,
					keep_head=config.FORGED_MESSAGE_MAX_CHARS - config.FORGED_MESSAGE_KEEP_TAIL,
					keep_tail=config.FORGED_MESSAGE_KEEP_TAIL,
				)
			elif len(text) > config.USER_MESSAGE_TRUNCATE_LENGTH:
				text = _elide_middle(
					text,
					keep_head=config.USER_MESSAGE_TRUNCATE_LENGTH - config.USER_MESSAGE_KEEP_TAIL,
					keep_tail=config.USER_MESSAGE_KEEP_TAIL,
				)
			message_texts.append(text)

		# P0-1: never silently drop queued messages beyond the per-step cap — the HITL
		# drain already caps at MAX_USER_MESSAGES_PER_STEP and leaves the rest queued,
		# but make it visible if a producer ever hands us more.
		_overflow = len(messages) - len(drained)
		if _overflow > 0:
			message_texts.append(f"[... {_overflow} more message(s) queued for the next step ...]")

		# Get session context (defaults if not provided)
		ctx = session_context or {}
		is_continuation = ctx.get('continuation', False)
		workspace_changes = ctx.get('workspace_changes')  # NEW

		# Build workspace change summary (NEW)
		change_summary = ""
		if workspace_changes and workspace_changes.has_changes():
			change_summary = workspace_changes.format_for_agent() + "\n\n"

		# Wall-clock stamp: stale frames from earlier turns must not be able to
		# masquerade as the current input (prod fa1212de: 9 undated PRIORITY
		# frames accumulated, indistinguishable from each other).
		received_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

		# P4: record the true source. Only an all-forged batch (self-wake /
		# delegation-result) is demoted; any genuine human kind keeps USER.
		kinds = {
			m.get('kind', 'comment')
			for m in messages[:config.MAX_USER_MESSAGES_PER_STEP]
		}
		if kinds and all(k in _FORGED_KIND_ORIGINS for k in kinds):
			guidance_origin = _FORGED_KIND_ORIGINS[next(iter(kinds))]
		else:
			guidance_origin = MessageOrigin.USER

		# Build frame. P3: an all-forged batch must NOT masquerade as the user
		# speaking — it gets an explicit autonomous re-entry frame instead of the
		# "NEW USER MESSAGE" one, so the model never "answers the user" a turn the
		# user never sent.
		if guidance_origin != MessageOrigin.USER:
			source_label = "self-wake" if guidance_origin == MessageOrigin.SELF_WAKE else "system"
			frame = f"""{change_summary}🤖 AUTONOMOUS RE-ENTRY ({source_label}, {received_at})

{chr(10).join(message_texts)}

This is NOT a new user message — it is a system-scheduled continuation. If there
is pending productive work, do it. Otherwise call done() briefly. Do NOT re-answer
previous user questions and do NOT send the user redundant status messages.
""".strip()
		elif is_continuation:
			# High-signal marker: User sent new message during/after task work
			user_request = "\n".join(message_texts)
			
			# Get task phase if available
			task_phase = ctx.get('task_phase', 1)
			prev_phase = task_phase - 1 if task_phase > 1 else 0
			
			# Build phase-aware continuation frame with memory override
			if task_phase > 1:
				# Multi-phase session - strong override required
				frame = f"""{change_summary}🔄 NEW USER MESSAGE - PRIORITY INPUT (PHASE {task_phase}, received {received_at})

⚠️ CRITICAL: NEW TASK PHASE STARTING ⚠️

Your previous memory may show task completion. THAT WAS PHASE {prev_phase}.
This is PHASE {task_phase} - a BRAND NEW task building on previous work.

User's NEW request for Phase {task_phase}:
{user_request}

MANDATORY MEMORY UPDATE:
First line MUST be: "Phase {task_phase}: {message_texts[0][:50] if message_texts else ''}... (NEW TASK)"
Then reference: "Phase {prev_phase} complete: [brief summary]"

INSTRUCTIONS:
1. {'Check workspace changes above - NEW FILES UPLOADED' if change_summary else 'Read your memory field to see what you have already done'}
2. {'If new files are relevant, READ THEM FIRST using filesystem_read_file()' if change_summary else 'Update memory as shown above (Phase {task_phase} at start)'}
3. Set next_goal to: "Begin Phase {task_phase}: {message_texts[0][:40] if message_texts else ''}..."
4. CRITICAL: Provide BOTH brain state JSON AND tool calls for the NEW work

THIS IS NOT A STATUS CHECK - IT'S NEW WORK USING PREVIOUS PHASE AS INPUT.
""".strip()
			else:
				# First continuation or phase tracking not available
				frame = f"""{change_summary}🔄 NEW USER MESSAGE - PRIORITY INPUT (received {received_at})

User sent you a new message:
{user_request}

INSTRUCTIONS:
1. {'⚠️ IMPORTANT: Check workspace changes above - user likely referring to NEW FILE' if change_summary else 'Read your memory field to see what you have already done'}
2. {'Read the new file(s) if relevant to the request' if change_summary else 'Incorporate this NEW USER MESSAGE into your next action immediately'}
3. Update your memory to reflect: "User requested: {message_texts[0][:40] if message_texts else ''}..."
4. CRITICAL: Provide BOTH brain state JSON (text content) AND tool calls

{'The user just uploaded new files - check them FIRST if they match the request.' if change_summary else 'This is a continuation of your session - your previous steps are in message history.'}
""".strip()
		else:
			# Mid-task guidance (less aggressive)
			frame = f"""{change_summary}User guidance ({received_at}): {chr(10).join(message_texts)}

Incorporate this guidance while maintaining brain state format (JSON + tool calls).
""".strip()

		# Apply overall token limit. P0-1: for a forged frame the closing
		# </untrusted_tool_result> delimiter sits near the END (before the trusted
		# footer), so a head-only `frame[:max_chars]` cut would strip it and leave the
		# tag open. Use head+tail elision and, for forged frames, a budget large enough
		# that a source-bounded wake payload passes intact.
		if guidance_origin != MessageOrigin.USER:
			max_chars = config.FORGED_MESSAGE_MAX_CHARS + 4000  # payload ceiling + frame overhead
			keep_tail = config.FORGED_MESSAGE_KEEP_TAIL
		else:
			max_chars = config.MAX_USER_GUIDANCE_TOKENS * 3  # ~3 chars per token
			keep_tail = config.USER_MESSAGE_KEEP_TAIL
		if len(frame) > max_chars:
			frame = _elide_middle(frame, keep_head=max_chars - keep_tail, keep_tail=keep_tail)

		# NEW: Check for image attachments and create multimodal message if present
		# FIXED: Collect images from ALL messages, not just first
		image_attachments = []
		image_sources = []  # Track which messages had images

		for idx, msg in enumerate(messages):
			msg_metadata = msg.get('metadata', {})
			if msg_metadata and 'image_attachments' in msg_metadata:
				imgs = msg_metadata['image_attachments']
				if imgs:  # Only process non-empty lists
					image_attachments.extend(imgs)
					image_sources.append(f"msg_{idx}")
					self.logger.info(f"📷 Found {len(imgs)} image(s) in message {idx}: {msg.get('text', '')[:50]}...")

		# Convert to None if empty (for consistency with existing code)
		if not image_attachments:
			image_attachments = None
		else:
			self.logger.info(
				f"📷 Total images collected: {len(image_attachments)} from {len(image_sources)} message(s) "
				f"(sources: {', '.join(image_sources)})"
			)

		# Create message (multimodal if images present)
		if image_attachments:
			# CX-M3: the initial task message stays pinned (it lives outside the
			# deque as a foundation message and is always prepended by
			# get_messages/get_messages_for_llm — see MessageManager.__init__ and
			# retrieval.py). Un-pinning it here (setting _initial_task_message to
			# None and merging self.task into this turn's text) broke the
			# "initial task is never evicted" invariant for the rest of the
			# session: the merge was a leftover defensive fix from before the P1
			# tail-append change below (this guidance message used to be inserted
			# near deque position 1; now it is always appended at the tail), so
			# there is no ordering hazard left to guard against — the foundation
			# (system, initial task, ...) is always emitted ahead of the whole
			# conversation deque regardless of where in the deque this message
			# lands.

			# Build multimodal content array: [text, image1, image2, ...]
			content_parts = [
				{'type': 'text', 'text': frame}
			]
			content_parts.extend(image_attachments)
			guidance_msg = HumanMessage(content=content_parts, origin=guidance_origin)

			# Log detailed structure (without full base64)
			content_summary = []
			for part in content_parts:
				if part.get('type') == 'text':
					text_preview = part['text'][:80] if len(part['text']) > 80 else part['text']
					content_summary.append(f"text({len(part['text'])} chars: \"{text_preview}...\")")
				elif part.get('type') == 'image_url':
					url_length = len(part.get('image_url', {}).get('url', ''))
					content_summary.append(f"image(data_url:{url_length} chars)")
				else:
					content_summary.append(f"unknown({part.get('type', 'no_type')})")

			self.logger.info(
				f"📷 Created multimodal user guidance:\n"
				f"   - Total parts: {len(content_parts)}\n"
				f"   - Images: {len(image_attachments)}\n"
				f"   - Structure: [{', '.join(content_summary)}]"
			)
		else:
			# Text-only message (existing behavior)
			guidance_msg = HumanMessage(content=frame, origin=guidance_origin)
			self.logger.debug("Created text-only user guidance (no images)")

		# P1 (2026-07-02): APPEND at the history tail. The frame historically went
		# to position 1 of the deque — near the TOP of the conversation. On a
		# resumed long-lived chat (prod fa1212de: 102 msgs) every real owner
		# message piled up at the top while the tail filled with stale
		# "Task Complete / no new input" turns, so the model kept dismissing
		# genuine new input. The newest user turn must be the newest message.
		self._add_message_with_tokens(guidance_msg)

		def _tail_msg():
			return self.history.messages[-1].message if self.history.messages else None

		# VERIFY MESSAGE WAS ADDED CORRECTLY (tail of history)
		added_msg = _tail_msg()
		if isinstance(added_msg, HumanMessage):
			if isinstance(added_msg.content, list):
				image_count_in_msg = sum(1 for p in added_msg.content if isinstance(p, dict) and p.get('type') == 'image_url')
				self.logger.info(f"✅ Multimodal message confirmed at history tail with {image_count_in_msg} image(s)")
			elif image_attachments:  # We expected images but got text
				self.logger.error(
					"🚨 BUG: Expected multimodal message but got text-only at history tail! "
					"Images were dropped during message creation."
				)
		else:
			self.logger.warning(f"⚠️ Unexpected message type at history tail: {type(added_msg).__name__}")

		# Log injection details
		self.logger.info(
			f"Injected user guidance at history tail "
			f"(continuation={is_continuation}, origin={guidance_origin}, "
			f"workspace_changes={bool(change_summary)})"
		)

		# Force recalibration to ensure we don't exceed limits
		# CRITICAL: This may modify messages - we verify afterward
		self.recalibrate_token_counts()

		# VERIFY MESSAGE SURVIVED RECALIBRATION
		post_recal_msg = _tail_msg()
		if isinstance(post_recal_msg, HumanMessage):
			if isinstance(post_recal_msg.content, list):
				image_count_after = sum(1 for p in post_recal_msg.content if isinstance(p, dict) and p.get('type') == 'image_url')
				if image_attachments and image_count_after < len(image_attachments):
					self.logger.error(
						f"🚨 IMAGE LOSS: Started with {len(image_attachments)} images, "
						f"only {image_count_after} remain after recalibration!"
					)
			elif image_attachments:
				self.logger.error(
					f"🚨 CRITICAL IMAGE LOSS: Multimodal guidance message was CONVERTED TO "
					f"TEXT during recalibration! {len(image_attachments)} image(s) LOST."
				)
		elif image_attachments:
			self.logger.error(
				f"🚨 CRITICAL: Guidance message lost from history tail during recalibration! "
				f"{len(image_attachments)} image(s) LOST."
			)


