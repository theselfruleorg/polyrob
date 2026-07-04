"""Memory-writing / action-summary mixin (code-motion from service.py)."""

from __future__ import annotations

import asyncio
import base64
import http
import importlib
import json
import logging
import os
import re
import subprocess
import traceback
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar, Union, Tuple
from collections import deque  # ADDED: Import deque for bounded collections
from agents.task.agent.tool_call_tracker import ToolCallTracker  # Robust tool call ID tracking

from dotenv import load_dotenv

# Import centralized constants
from agents.task.constants import (
    IMG_TOKENS,
    LoopDetectionConfig,
    DEFAULT_USER_ID,
    MemoryConfig,
    MAX_MCP_PER_STEP
)

# Import POLYROB exceptions
from core.exceptions import (
    AgentError,
    ValidationError as ROBValidationError,
    LLMResponseError,
    ToolError
)

# Import billing exception for fail-fast handling
from core.exceptions import InsufficientCreditsError

# Import centralized utilities to avoid repeated inline imports
from agents.task.utils_json import normalize_action_schema
# Model limits now come from modules.llm.model_registry

# Native message types
from modules.llm.messages import (
	AIMessage,
	BaseMessage,
	HumanMessage,
	SystemMessage,
	ToolMessage,
)
from modules.llm.adapters import BaseChatModel
from core.exceptions import (
    RateLimitError,
    LLMError,
    LLMRateLimitError,
    LLMAuthenticationError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMResponseError,
    LLMPermanentError,
    LLMProviderExhaustedError
)
# PIL Image imported locally in save_screenshot() method where needed
from pydantic import BaseModel, ConfigDict, ValidationError

from tools.browser.views import BrowserStateHistory, BrowserState
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt, AgentMessagePrompt
from agents.task.agent.views import (
    ActionModel,
    AgentBrain,
    AgentError,
    AgentHistory,
    AgentHistoryList,
    AgentOutput,
    AgentStepInfo,
    ActionResult,
)
from tools.browser.context import BrowserContext
from tools.dom.views import DOMElementNode, SelectorMap
from agents.task.telemetry.views import (
    HumanApprovalRequestedEvent,
    HumanApprovalDecisionEvent,
    TodoStatusEvent,
    AgentRunTelemetryEvent,
    AgentEndTelemetryEvent,
    ProviderFailureEvent,
    ProviderFallbackSuccessEvent,
)
# ProductTelemetry no longer directly used - accessed via TelemetryManager
from agents.task.utils import time_execution_async, detect_llm_provider, extract_token_usage
# Safely import Google API exceptions
try:
    from google.api_core.exceptions import ResourceExhausted
except ImportError:
    # Create a dummy exception class if google-api-core is not installed
    class ResourceExhausted(Exception):
        pass

# Import from our own logging config
from agents.task.logging_config import get_task_logger

# Import centralized path management
from agents.task.path import pm



class MemoryWriterMixin:
	"""Mixin extracted verbatim from agents/task/agent/service.py (pure code-motion)."""

	def _extract_progress_from_memory(self, memory: Optional[str]) -> Optional[str]:
		"""Extract progress tracking from memory string.

		Args:
			memory: Memory string that may contain progress like "Progress: 3/10"

		Returns:
			Progress string (e.g., "3/10") or None if not found
		"""
		if not memory:
			return None
		import re
		match = re.search(r'Progress:\s*(\d+/\d+)', memory)
		return match.group(1) if match else None

	def _build_memory_from_actions(
		self,
		step_number: int,
		action_summaries: List[str],
		previous_memory: Optional[str] = None,
		previous_progress: Optional[str] = None
	) -> str:
		"""Build memory string from action results.

		Generates a natural memory description from action results when the LLM
		doesn't provide one directly.

		Args:
			step_number: Current step number (for context, not used in output format)
			action_summaries: List of action summary strings (e.g., "action_name→result")
			previous_memory: Previous memory narrative (unused - each step is fresh)
			previous_progress: Optional progress tracking from previous step (e.g., "3/10")

		Returns:
			Natural memory string describing what happened
		"""
		# Part 1: What I JUST did (action taken)
		if action_summaries:
			action_desc = ". ".join(action_summaries[:2])  # First 2 actions for brevity
			if len(action_summaries) > 2:
				action_desc += f" + {len(action_summaries)-2} more actions"
		else:
			action_desc = "No actions taken"

		# Part 2: What I learned (from results)
		learned_parts = []
		for summary in action_summaries[:3]:
			if "→ERROR" in summary:
				error_part = summary.split("→ERROR:")[1] if "→ERROR:" in summary else "action failed"
				learned_parts.append(f"Error: {error_part[:50]}")
			elif "→DONE" in summary:
				learned_parts.append("Task complete")
			elif "→" in summary:
				result = summary.split("→")[1]
				if result and result != "OK":
					learned_parts.append(f"Found: {result[:60]}")

		learned = " ".join(learned_parts[:2]) if learned_parts else ""

		# Part 3: Progress tracking
		progress_part = ""
		if previous_progress:
			progress_part = f" Progress: {previous_progress}."

		# Combine into natural format
		memory = action_desc
		if learned:
			memory += f". {learned}"
		if progress_part:
			memory += f".{progress_part}"

		return memory

	async def _save_step_to_memory(
		self,
		step_number: int,
		brain_state: Dict[str, Any],
		actions: List[Dict[str, Any]],
		results: List[Any],
		step_info: Optional[Any] = None
	) -> None:
		"""Save step to hierarchical memory with brain state memory field.

		CRITICAL: The brain_state.memory field contains the agent's cumulative
		understanding and should be the PRIMARY source for H-MEM findings.

		Args:
			step_number: Current step number
			brain_state: Brain state from LLM (contains 'memory', 'phase', etc.)
			actions: Actions taken this step
			results: Action results
			step_info: Optional AgentStepInfo with max_steps
		"""
		if not self.task_context_manager or not self.session_id:
			self.logger.debug("H-MEM unavailable: task_context_manager or session_id missing")
			return

		try:
			# Build action summary
			action_summary = self._build_action_summary(actions)

			# Extract memory from brain_state.memory (PRIMARY source)
			raw_memory = brain_state.get('memory', '').strip()

			# CO-F4 / A1: the "Synthesis pending..." placeholder brain is not a real
			# finding — it must be treated as empty for BOTH the H-MEM write (handled
			# by the fallback at the bottom) AND the loop-duplicate heuristic. Setting
			# or comparing _last_memory_finding against the placeholder would let two
			# consecutive placeholder steps perturb the thinking-loop signal.
			is_placeholder = raw_memory.startswith("Synthesis pending")

			if raw_memory and not is_placeholder:
				finding = raw_memory

				# Check for duplicate (static memory = thinking loop symptom)
				if hasattr(self, '_last_memory_finding') and finding == self._last_memory_finding:
					self.logger.warning(
						f"⚠️ Memory identical to previous step - thinking loop indicator"
					)
					# Add warning to next step
					try:
						# HumanMessage imported at module level from modules.llm.messages
						self.message_manager.push_ephemeral_message(HumanMessage(
							content=(
								"⚠️ Your memory is identical to the previous step.\n"
								"This suggests you're not making progress.\n\n"
								"Memory should be unique each step:\n"
								"- What you DID this step (actions taken)\n"
								"- What you LEARNED (new insights)\n"
								"- Progress update (X/Y if quantitative)"
							)
						))
					except Exception as e:
						self.logger.warning(f"Could not inject memory warning: {e}")
				else:
					# Save for next comparison
					self._last_memory_finding = finding
			else:
				finding = None

			# Fallback: If memory field empty OR still the early-exit placeholder
			# brain (CO-F4 — "Synthesis pending..." must never land in H-MEM as a
			# real finding), try action results (secondary source).
			if not finding or len(finding) < 10 or finding.startswith("Synthesis pending"):
				self.logger.debug(f"Brain state memory empty at step {step_number}, using action results")
				result_finding = self._extract_finding_from_results(results)
				if result_finding:
					finding = result_finding
				else:
					# Last resort: Use next_goal as finding
					finding = brain_state.get('next_goal', 'Step in progress')

			# Get total_steps from step_info if available
			total_steps = step_info.max_steps if step_info and hasattr(step_info, 'max_steps') else None

			# Add to hierarchical memory.
			# HIGH-3: offload off the event loop — add_step_memory is synchronous and, when
			# REFLECTION_LLM_ENABLED=true, blocks on an aux-LLM call (run_coroutine_sync, up to
			# 30s) that would otherwise freeze every concurrent session in this process. We await
			# the result so per-session ordering is preserved (mirrors _save_conversation).
			success = await asyncio.to_thread(
				self.task_context_manager.add_step_memory,
				session_id=self.session_id,
				step=step_number,
				brain_state=brain_state,
				action_summary=action_summary,
				finding=finding,  # Now uses brain_state.memory!
				total_steps=total_steps
			)

			if success:
				# INFO level so we can see H-MEM working in production
				self.logger.info(f"💾 H-MEM saved step {step_number}: {finding[:80]}...")
				
				# FIX #4: Track finding in AgentState for loop detection
				self.state.track_finding()
				
				# Validate it's actually in H-MEM
				try:
					session_data = self.task_context_manager.get_session(self.session_id)
					if session_data:
						phase_memory = session_data.memory.get_current_phase_memory()
						if phase_memory:
							finding_count = len(phase_memory.key_findings)
							self.logger.debug(
								f"✅ H-MEM validation: Phase '{phase_memory.phase_name}' "
								f"now has {finding_count} findings"
							)
				except Exception as val_error:
					self.logger.debug(f"H-MEM validation check failed: {val_error}")
			else:
				self.logger.warning(f"⚠️  H-MEM save returned False for step {step_number}")

			# Periodically save to disk (every 10 steps)
			if step_number % 10 == 0:
				self.task_context_manager.save_session(self.session_id, self.user_id)
				self.logger.info(f"💾 Persisted H-MEM to disk (step {step_number})")

		except Exception as e:
			self.logger.error(f"Failed to save step to hierarchical memory: {e}", exc_info=True)

		# P7: also route the completed step through the active MemoryProvider.
		# Cross-session store ingests H-MEM's curated/deduped findings (drain returns
		# only findings new since the last call), NOT the raw per-step brain memory.
		# No-op unless an external provider is registered; isolated + fail-open.
		try:
			from modules.memory.registry import memory_sync_turn
			task_str = getattr(self, 'task', '') or ''
			promoted = []
			if self.task_context_manager and self.session_id:
				promoted = self.task_context_manager.drain_promoted_findings(self.session_id)
			if promoted:
				content = "\n".join(promoted)
				await memory_sync_turn(task_str, content,
				                       session_id=self.session_id, user_id=self.user_id)
		except Exception as e:
			self.logger.debug(f"memory_sync_turn skipped (backend hiccup): {e}")

	def _build_action_summary(self, actions: List[Dict[str, Any]]) -> str:
		"""Build human-readable action summary.

		Args:
			actions: List of actions taken

		Returns:
			Summary string
		"""
		if not actions:
			return "No actions taken"

		summaries = []
		for action in actions:
			action_type = action.get('action_type', 'unknown')
			# Get first key if action is dict
			if isinstance(action, dict) and action:
				first_key = list(action.keys())[0]
				summaries.append(first_key)
			else:
				summaries.append(action_type)

		return f"Executed: {', '.join(summaries)}"

	def _extract_finding_from_results(self, results: List[Any]) -> Optional[str]:
		"""Extract key finding from action results.

		Args:
			results: Action results

		Returns:
			Finding string or None
		"""
		if not results:
			return None

		# Look for significant results
		for result in results:
			# Handle ActionResult objects
			if hasattr(result, 'error') and result.error:
				return f"Error: {result.error[:100]}"

			# Check for extracted data
			if hasattr(result, 'extracted_content') and result.extracted_content:
				if len(result.extracted_content) > 100:
					return f"Found: {result.extracted_content[:200]}..."

		return None

	def _extract_intelligent_preview(self, content: str, max_length: int = 10000) -> str:
		"""
		Extract intelligent preview from content with context-aware summarization.
		For HTML: Extract structured data (title, headings, key info)
		For other: Clean and compact whitespace
		Based on 2025 research: Query-focused summarization for tool outputs.
		"""
		import re

		# Detect if HTML
		is_html = any(marker in content[:500].lower() for marker in ['<html', '<!doctype', '<head', '<body'])

		if is_html:
			# HTML-specific extraction
			try:
				parts = []

				# Extract title
				title_match = re.search(r'<title[^>]*>(.*?)</title>', content, re.IGNORECASE | re.DOTALL)
				if title_match:
					title = re.sub(r'\s+', ' ', title_match.group(1)).strip()
					parts.append(f"Title: {title}")

				# Extract meta description
				desc_match = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)', content, re.IGNORECASE)
				if desc_match:
					parts.append(f"Description: {desc_match.group(1)}")

				# Extract h1 headings (max 3)
				h1_matches = re.findall(r'<h1[^>]*>(.*?)</h1>', content, re.IGNORECASE | re.DOTALL)
				if h1_matches:
					h1_clean = [re.sub(r'<[^>]+>|&\w+;|\s+', ' ', h.strip()) for h in h1_matches[:3]]
					h1_clean = [h for h in h1_clean if h and len(h) > 5]
					if h1_clean:
						parts.append(f"Main Headings: {'; '.join(h1_clean)}")

				# Extract h2 headings (max 5)
				h2_matches = re.findall(r'<h2[^>]*>(.*?)</h2>', content, re.IGNORECASE | re.DOTALL)
				if h2_matches:
					h2_clean = [re.sub(r'<[^>]+>|&\w+;|\s+', ' ', h.strip()) for h in h2_matches[:5]]
					h2_clean = [h for h in h2_clean if h and len(h) > 5]
					if h2_clean:
						parts.append(f"Sections: {'; '.join(h2_clean)}")

				# Extract first paragraph of actual content (skip nav/header)
				# Look for <p> tags after <main>, <article>, or <div class="content">
				content_areas = re.findall(r'<(?:main|article|div[^>]*(?:class|id)=["\'][^"\']*content[^"\']*["\'])[^>]*>(.*?)</(?:main|article|div)>',
										   content, re.IGNORECASE | re.DOTALL)
				if content_areas:
					for area in content_areas[:1]:
						p_matches = re.findall(r'<p[^>]*>(.*?)</p>', area, re.IGNORECASE | re.DOTALL)
						if p_matches:
							first_p = re.sub(r'<[^>]+>|&\w+;|\s+', ' ', p_matches[0]).strip()
							if len(first_p) > 50:
								parts.append(f"Content: {first_p[:300]}...")
								break

				# Combine parts
				if parts:
					preview = ' | '.join(parts)
					if len(preview) > max_length:
						preview = preview[:max_length] + "..."
					return preview

			except Exception as e:
				# Fallback to simple preview if parsing fails
				pass

		# Non-HTML or fallback: Clean whitespace and compact
		preview = re.sub(r'\s+', ' ', content[:max_length]).strip()
		if len(content) > max_length:
			preview += "..."
		return preview

	def _handle_large_action_results(self, results: List[ActionResult]) -> None:
		"""Handle large content in ActionResults to prevent memory issues."""
		from agents.task.robust_parse_config import RobustParseConfig
		import time
		import re
		
		for result in results:
			if result.extracted_content and len(result.extracted_content) > RobustParseConfig.MAX_EXTRACTED_CONTENT_SIZE:
				try:
					# Store original content length before replacement
					original_content_length = len(result.extracted_content)
					
					# Try to store large content in a file and replace with reference
					from agents.task.path import pm
					
					# FIXED: Generate intelligent filename based on action context and content type
					filename = None
					content_preview = result.extracted_content[:500]  # First 500 chars for analysis
					
					# Enhanced filename generation based on content analysis
					if hasattr(result, 'action_type'):
						action_type = result.action_type
					elif hasattr(result, 'action_name'):
						action_type = result.action_name
					else:
						action_type = 'content'
					
					# Analyze content to determine appropriate extension
					file_extension = '.txt'  # Default
					if any(marker in content_preview.lower() for marker in ['<html', '<!doctype', '<head', '<body']):
						file_extension = '.html'
					elif any(marker in content_preview for marker in ['{', '}', '[', ']', '":']):
						# Likely JSON
						file_extension = '.json'
					elif content_preview.strip().startswith('<?xml'):
						file_extension = '.xml'
					elif '|' in content_preview and content_preview.count('\n') > 3:
						# Looks like tabular data
						file_extension = '.csv'
					
					# FIXED: Create more descriptive filename with timestamp and content hash
					import hashlib
					content_hash = hashlib.md5(result.extracted_content.encode()).hexdigest()[:8]
					timestamp = int(time.time())
					
					if hasattr(result, 'metadata') and result.metadata and 'url' in result.metadata:
						url = result.metadata['url']
						# Convert URL to safe filename component
						safe_url = re.sub(r'[^\w\-_.]', '_', url.replace('https://', '').replace('http://', ''))
						safe_url = safe_url[:50]  # Limit length
						filename = f"{action_type}_{safe_url}_{timestamp}_{content_hash}{file_extension}"
					else:
						filename = f"{action_type}_{timestamp}_{content_hash}{file_extension}"
					
					# FIXED: Store in workspace root so filesystem can access it
					# Filesystem enforces workspace-only access, so files must be in workspace/
					content_file = pm().create_file_path(
						self.session_id,
						"workspace",
						filename,
						user_id=self.user_id
					)
					
					# FIXED: Write content with proper encoding and error handling
					try:
						with open(content_file, 'w', encoding='utf-8', errors='replace') as f:
							f.write(result.extracted_content)
					except UnicodeEncodeError:
						# Fallback: write as bytes if UTF-8 fails
						with open(content_file, 'wb') as f:
							f.write(result.extracted_content.encode('utf-8', errors='replace'))
					
					# FIXED: Create enhanced file reference with explicit agent instructions
					# Build metadata section
					metadata_parts = []
					if hasattr(result, 'metadata') and result.metadata:
						if 'url' in result.metadata:
							metadata_parts.append(f"Source: {result.metadata['url']}")
						if 'title' in result.metadata:
							metadata_parts.append(f"Title: {result.metadata['title'][:100]}")
						if 'content_type' in result.metadata:
							metadata_parts.append(f"Type: {result.metadata['content_type']}")

					metadata_str = f" | {' | '.join(metadata_parts)}" if metadata_parts else ""

					# Build file reference with EXPLICIT agent instructions for accessing stored content
					file_reference = f"""[LARGE CONTENT STORED]
File: {content_file.name}
Size: {original_content_length:,} characters{metadata_str}

HOW TO ACCESS: Use the `read_file` action with file_path="{content_file.name}" to read the full content.
Example: {{"read_file": {{"file_path": "{content_file.name}"}}}}
"""

					# Add intelligent preview with context-aware summarization
					preview_length = RobustParseConfig.LARGE_CONTENT_PREVIEW_LENGTH
					if preview_length > 0:
						# Use intelligent extraction for HTML/structured content
						preview = self._extract_intelligent_preview(result.extracted_content, max_length=preview_length)
						file_reference += f"\nPREVIEW (first {len(preview):,} chars):\n{preview}"

					file_reference += "\n[END LARGE CONTENT REFERENCE]"
					
					# FIXED: Store file reference metadata for better tracking
					# Initialize file_references if None or not a list
					if not isinstance(getattr(result, 'file_references', None), list):
						result.file_references = []

					file_ref_metadata = {
						'type': 'large_content',
						'path': str(content_file),
						'original_size': original_content_length,
						'preview_size': len(file_reference),
						'content_type': file_extension[1:],  # Remove the dot
						'created_at': time.time()
					}

					if hasattr(result, 'metadata') and result.metadata:
						file_ref_metadata['source_metadata'] = result.metadata

					result.file_references.append(file_ref_metadata)
					
					# Replace with enhanced file reference
					result.extracted_content = file_reference
					
					# Log with better context
					self.logger.info(f"Stored large {action_type} content ({original_content_length:,} chars) in {content_file.name}")
					
				except Exception as e:
					self.logger.warning(f"Failed to store large content in file: {e}", exc_info=True)
					# Fallback to simple truncation using new config
					result.extracted_content = RobustParseConfig.truncate_extracted_content(result.extracted_content)

					# FIXED: Still create file reference metadata for fallback case
					# Initialize file_references if None or not a list
					if not isinstance(getattr(result, 'file_references', None), list):
						result.file_references = []

					result.file_references.append({
						'type': 'truncated_content',
						'original_size': len(result.extracted_content) + len(' [TRUNCATED]'),
						'truncated_size': len(result.extracted_content),
						'reason': f'File storage failed: {str(e)}'
					})

