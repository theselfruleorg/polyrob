"""
AutoV2 Telemetry Service

Provides telemetry capture for the AutoV2 agent framework with optimized
event buffering, feed directory management, and LLM usage tracking.
"""

import logging
import os
import uuid
import json
from pathlib import Path
from typing import Optional, Dict, Any, Union, List
import time
from datetime import datetime
import traceback
import threading

from dotenv import load_dotenv

# Try to import Posthog, but don't fail if it's not available
try:
    from posthog import Posthog
    POSTHOG_AVAILABLE = True
except ImportError:
    Posthog = None
    POSTHOG_AVAILABLE = False

from agents.task.telemetry.views import BaseTelemetryEvent, LLMRequestTelemetryEvent, AgentStepTelemetryEvent
from agents.task.telemetry.sequence import SequenceGenerator, generate_event_id, get_timestamp_ms
from agents.task.utils import SafeFileLock
from agents.task.path import get_safe_singleton
from agents.task.path import pm

# First try to import filelock package (no longer needed as we use SafeFileLock)
# The existing FileLock class is removed since we use SafeFileLock from utils

# Import logger
from agents.task.logging_config import get_task_logger
logger = get_task_logger('telemetry')

load_dotenv()


POSTHOG_EVENT_SETTINGS = {
	'process_person_profile': True,
}


class ProductTelemetry:
	"""
	Service for capturing anonymized telemetry data.

	If the environment variable `ANONYMIZED_TELEMETRY=False`, anonymized telemetry will be disabled.
	"""

	USER_ID_PATH = str(Path.home() / '.cache' / 'auto' / 'telemetry_user_id')
	# Read API key from environment with fallback for development
	PROJECT_API_KEY = os.getenv('POSTHOG_API_KEY', 'phx_dev_fallback_key')
	HOST = 'https://us.i.posthog.com'
	UNKNOWN_USER_ID = 'UNKNOWN'

	_curr_user_id = None

	# Class-level feed callback — set by CLI to receive all feed events.
	# Signature: (session_id: str, event: dict) -> None
	_on_feed_entry = None

	def __init__(self) -> None:
		# CRITICAL FIX: Separate Posthog analytics from feed writing
		# Feed writing is ALWAYS enabled (required for webview)
		# Posthog analytics can be disabled with ANONYMIZED_TELEMETRY=false
		posthog_telemetry_value = os.getenv('ANONYMIZED_TELEMETRY', 'true').lower()
		self.posthog_enabled = posthog_telemetry_value == 'true'

		# Feed writing is always enabled for webview functionality
		self.feed_writing_enabled = True

		self.debug_logging = os.getenv('BROWSER_USE_LOGGING_LEVEL', 'info').lower() == 'debug'

		# Initialize logger with the global logger to avoid duplicates
		try:
			self.logger = logger
		except NameError:
			# Fallback if logger wasn't successfully initialized at the module level
			self.logger = logging.getLogger('task.telemetry')

		if not self.posthog_enabled:
			self._posthog_client = None
			self.logger.debug('Posthog telemetry disabled (feed writing still enabled)')
		elif not POSTHOG_AVAILABLE:
			self._posthog_client = None
			self.logger.debug('Posthog not available (feed writing still enabled)')
		else:
			# Log at debug level to avoid duplicate messages
			self.logger.debug('Posthog telemetry enabled')
			self._posthog_client = Posthog(
				project_api_key=self.PROJECT_API_KEY,
				host=self.HOST,
				disable_geoip=False,
			)

			# Silence posthog's logging
			if not self.debug_logging:
				posthog_logger = logging.getLogger('posthog')
				posthog_logger.disabled = True
				
		# Initialize sensitive data filter
		self.sensitive_data = {}
		try:
			# Try to load sensitive data from environment
			from os import environ
			for key, value in environ.items():
				if key.startswith("API_KEY_") or key.endswith("_KEY") or key.endswith("_SECRET") or "_TOKEN" in key:
					if value and len(value) > 5:
						self.sensitive_data[key] = value
		except Exception as e:
			self.logger.debug(f"Failed to initialize sensitive data filter: {e}")

		# FIXED: Add telemetry optimization configuration
		self._event_buffer = {}  # Buffer events by session_id
		self._buffer_size = int(os.getenv('TELEMETRY_BUFFER_SIZE', '500'))  # Increased buffer size
		self._buffer_timeout = float(os.getenv('TELEMETRY_BUFFER_TIMEOUT', '60.0'))  # Increased flush timeout
		self._last_flush_times = {}  # Track last flush time per session
		self._buffer_lock = threading.RLock()  # Thread safety for buffer operations

		# Feed retention wiring (telemetry audit 2026-07-04): _enforce_feed_retention
		# was dead code; drive it from the feed writer, throttled every N writes so a
		# long-running session's feed/ dir stays bounded instead of growing forever.
		self._feed_write_counts = {}
		self._feed_retention_every = max(1, int(os.getenv('TELEMETRY_FEED_RETENTION_EVERY', '50')))

		# NEW: Add health monitoring statistics
		self._health_stats = {
			'total_captures': 0,
			'failed_captures': 0,
			'duplicate_requests': 0,
			'token_estimation_fallbacks': 0,
			'successful_llm_captures': 0,
			'failed_llm_captures': 0,
			'last_capture_time': None,
			'startup_time': time.time()
		}

	def _is_telemetry_enabled(self) -> bool:
		"""Check if telemetry is enabled (Posthog analytics OR feed writing).

		Returns:
			True if any form of telemetry is enabled, False otherwise
		"""
		# Telemetry is enabled if EITHER Posthog OR feed writing is enabled
		return self.posthog_enabled or self.feed_writing_enabled

	def _should_save_to_file(self) -> bool:
		"""Check if telemetry should be saved to feed files.

		Returns:
			True if feed writing is enabled, False otherwise
		"""
		# CRITICAL FIX: Feed writing should always be enabled for webview
		return self.feed_writing_enabled

	def _get_path_for_event(self, event: BaseTelemetryEvent) -> Path:
		"""Get the path to save the event."""
		# Get session ID
		session_id = None
		if hasattr(event, 'get_session_id'):
			session_id = event.get_session_id()
		elif hasattr(event, 'session_id'):
			session_id = event.session_id
		elif hasattr(event, 'agent_id') and '_' in event.agent_id:
			# Try to extract session ID from agent_id
			parts = event.agent_id.split('_', 1)
			if len(parts) > 1:
				session_id = parts[1]
				
		if not session_id:
			# Default to a global telemetry file if no session ID
			return pm().data_root / f"global_{event.name}.jsonl"
			
		# Clean the session ID for consistency
		cleaned_id = pm().clean_session_id(session_id)
		
		# Use PathManager's canonical telemetry directory
		telemetry_dir = pm().get_telemetry_dir(cleaned_id)
		return telemetry_dir / f"{event.name}.jsonl"
			
	def _get_session_path_for_event(self, event: BaseTelemetryEvent) -> Path:
		"""Get the session path for the event."""
		session_id = None
		if hasattr(event, 'get_session_id'):
			session_id = event.get_session_id()
		elif hasattr(event, 'session_id'):
			session_id = event.session_id
		elif hasattr(event, 'agent_id') and '_' in event.agent_id:
			# Try to extract session ID from agent_id
			parts = event.agent_id.split('_', 1)
			if len(parts) > 1:
				session_id = parts[1]
		
		if not session_id:
			return pm().data_root
		
		# Clean the session ID and get root path
		cleaned_id = pm().clean_session_id(session_id)
		return pm().get_session_root(cleaned_id)

	def _ensure_telemetry_dir(self, session_id: str) -> Optional[Path]:
		"""Ensure telemetry directory exists for the given session.
		
		Args:
			session_id: The session ID (already cleaned)
			
		Returns:
			Path to the telemetry directory or None if creation failed
		"""
		try:
			# Clean session ID for consistency
			clean_id = pm().clean_session_id(session_id)
			
			# Get telemetry directory using PathManager's canonical method
			telemetry_dir = pm().get_telemetry_dir(clean_id)
			return telemetry_dir
		except Exception as e:
			self.logger.error(f"Failed to create telemetry directory: {e}", exc_info=True)
			return None

	def _sanitize_telemetry_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
		"""Sanitize telemetry data to remove sensitive information.
		
		Args:
			data: The telemetry data to sanitize
			
		Returns:
			Sanitized telemetry data
		"""
		if not data:
			return {}
			
		# Create a deep copy to avoid modifying the original
		import copy
		sanitized = copy.deepcopy(data)
		
		# Refresh sensitive data from environment to catch runtime-injected values
		self._refresh_sensitive_data()
		
		def _sanitize_value(value: Any) -> Any:
			"""Recursively sanitize a value"""
			if isinstance(value, dict):
				# Special handling for parameter dictionaries which often contain credentials
				sanitized_dict = {}
				for k, v in value.items():
					# Check dictionary keys for sensitive patterns
					lower_key = str(k).lower()
					# List of sensitive key substrings. Keep this explicit to avoid over-filtering.
					_sensitive_key_markers = [
					    'api_key', 'apikey', 'access_key', 'access_token', 'client_secret',
					    'secret', 'password', 'credential', 'auth_token', 'bearer_token'
					]
					# Certain telemetry keys legitimately contain the word "token" (e.g. "token_count").
					# Whitelist those so they are not redacted.
					_safe_token_keys = {
					    'token_count', 'prompt_tokens', 'completion_tokens', 'total_tokens',
					    'input_tokens', 'output_tokens', 'usage_tokens'
					}
					if lower_key not in _safe_token_keys and any(marker in lower_key for marker in _sensitive_key_markers):
						# This is likely a sensitive field, redact the value completely
						sanitized_dict[k] = '<REDACTED>'
					elif lower_key.endswith('_tokens') or lower_key.endswith('_count') or 'token' in lower_key:
						# Handle numeric token fields specially to ensure they're preserved
						if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
							# Preserve numeric token count fields
							sanitized_dict[k] = v
						else:
							# Recursively sanitize non-numeric values
							sanitized_dict[k] = _sanitize_value(v)
					else:
						# Recursively sanitize the value
						sanitized_dict[k] = _sanitize_value(v)
				return sanitized_dict
			elif isinstance(value, list):
				return [_sanitize_value(item) for item in value]
			elif isinstance(value, str):
				# Redact sensitive data
				sanitized_str = value
				
				# First check known sensitive values from environment
				for key, sensitive_value in self.sensitive_data.items():
					if sensitive_value in sanitized_str:
						sanitized_str = sanitized_str.replace(sensitive_value, f"<REDACTED_{key}>")
				
				# Check for common credential patterns that might indicate sensitive data
				import re
				
				# API keys, tokens, and secrets (typically long alphanumeric strings)
				patterns = [
					# API keys/tokens with identifier
					r'(api[_-]?key|token|secret|password|apikey|auth)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-\.]{20,})["\']?',
					# Bearer tokens in Authorization headers
					r'(bearer\s+)([a-zA-Z0-9_\-\.]{20,})',
					# API keys in URLs
					r'(key=|token=|api_key=|apikey=)([a-zA-Z0-9_\-\.]{20,})',
					# AWS-style keys (alphanumeric with fixed length)
					r'(AKIA[0-9A-Z]{16})',
					# Typical API key patterns
					r'(sk-[a-zA-Z0-9]{20,})',  # OpenAI keys
					r'(gh[pousr]_[A-Za-z0-9_]{36})',  # GitHub tokens
					r'(SG\.[a-zA-Z0-9_\-\.]{20,})',  # SendGrid
					r'(AIza[0-9A-Za-z\-_]{35})',  # Google API
					r'(ya29\.[0-9A-Za-z\-_]+)'   # Google OAuth
				]
				
				for pattern in patterns:
					if re.search(pattern, sanitized_str, re.IGNORECASE):
						sanitized_str = re.sub(pattern, r'\1<REDACTED>', sanitized_str, flags=re.IGNORECASE)
				
				return sanitized_str
			else:
				# Handle non-serializable objects by converting them to a safe format
				try:
					# Test if JSON-serializable
					json.dumps(value)
					return value
				except (TypeError, ValueError, OverflowError):
					# Not directly serializable, use our helper method
					return self._json_serializable(value)
				
		# Apply sanitization recursively to the entire data structure
		return _sanitize_value(sanitized)

	def _refresh_sensitive_data(self) -> None:
		"""Refresh the sensitive data dictionary from current environment.
		This allows catching API keys and tokens injected at runtime.
		"""
		try:
			# Get current environment variables
			from os import environ
			
			# Look for new or updated sensitive values
			for key, value in environ.items():
				# Check common patterns for sensitive environment variables
				if (key.startswith("API_KEY_") or key.endswith("_KEY") or 
					key.endswith("_SECRET") or "_TOKEN" in key or
					key.endswith("_PASSWORD") or "_CREDENTIAL" in key):
					if value and len(value) > 5:
						# Only add if value is non-trivial and potentially sensitive
						self.sensitive_data[key] = value
		except Exception as e:
			self.logger.debug(f"Failed to refresh sensitive data: {e}")

	def _json_serializable(self, obj: Any) -> Any:
		"""Convert an object to a JSON-serializable form.
		
		Args:
			obj: The object to convert
			
		Returns:
			JSON-serializable version of the object
		"""
		if isinstance(obj, (datetime, Path)):
			return str(obj)
		elif hasattr(obj, "model_dump"):
			try:
				return obj.model_dump()
			except Exception:
				return str(obj)
		elif hasattr(obj, "__dict__"):
			try:
				return {k: self._json_serializable(v) for k, v in obj.__dict__.items()
						if not k.startswith('_')}
			except Exception:
				return str(obj)
		else:
			return str(obj)

	def capture(self, event: BaseTelemetryEvent, session_id: Optional[str] = None) -> None:
		"""Capture a telemetry event.
		
		Args:
			event: The event to capture
			session_id: Optional session ID override
		"""
		# Get event name and properties
		event_name = event.name
		event_properties = event.properties
		
		# Try to extract session_id from event if not provided
		if not session_id:
			# Try event's get_session_id() method first
			if hasattr(event, 'get_session_id') and callable(getattr(event, 'get_session_id')):
				session_id = event.get_session_id()
				
			# Then try common event attributes
			if not session_id:
				for attr in ['session_id', 'agent_id', 'parent_session_id']:
					if hasattr(event, attr) and getattr(event, attr):
						session_id = getattr(event, attr)
						break
						
			# Finally try to extract from agent_id if it has session ID embedded
			if not session_id and hasattr(event, 'agent_id') and getattr(event, 'agent_id'):
				agent_id = getattr(event, 'agent_id')
				if '_' in agent_id:
					session_id = agent_id.split('_', 1)[1]
		
		# CRITICAL FIX: Clean session ID once we've extracted it
		if session_id:
			from agents.task.path import pm
			try:
				session_id = pm().clean_session_id(session_id)
			except Exception as e:
				self.logger.debug(f"Failed to clean session ID: {e}")
		
		# Use primary session persistence mechanism
		if session_id:
			# Check if telemetry is enabled
			if not self._is_telemetry_enabled():
				return
			
			# Extract agent_id from event properties if available
			agent_id = None
			if hasattr(event, 'agent_id'):
				agent_id = event.agent_id
			elif hasattr(event, 'properties') and 'agent_id' in event.properties:
				agent_id = event.properties['agent_id']
			
			# Direct capture for efficiency when we don't need to save to disk
			if not self._should_save_to_file():
				self._direct_capture(event)
				return
			
			# Local telemetry saving to session file
			try:
				# Get properties and sanitize
				# Get properties from event (it's a @property, not a method)
				properties = {}
				if hasattr(event, 'properties'):
					properties = event.properties
				
				# Sanitize sensitive data
				sanitized_properties = self._sanitize_telemetry_data(properties)
				
				# Create a clean telemetry event dictionary with timestamp and metadata
				telemetry_event = {
					"name": event.name,
					"timestamp": time.time(),
					"datetime": datetime.now().isoformat(),
					"properties": sanitized_properties,
					"agent_id": agent_id
				}
				
				# Get telemetry directory ensuring proper path handling
				telemetry_dir = self._ensure_telemetry_dir(session_id)
				if not telemetry_dir:
					self.logger.error(f"Could not create telemetry directory for session {session_id}")
					return
					
				# FIXED: Use buffered writing to reduce filesystem I/O
				# Add event to buffer instead of writing immediately
				self._add_event_to_buffer(session_id, telemetry_event)
				
				self.logger.debug(f"Telemetry event buffered for session: {session_id}")
				
				# Persist the event into the session-specific *feed* directory so
				# that real-time components (web UI, dashboards, etc.) can
				# consume it. We no longer restrict which events qualify – the
				# downstream formatter will gracefully ignore unknown types if
				# it does not recognise them.
				self._save_to_feed_directory(event, session_id)
				
			except Exception as e:
				self.logger.error(f"Failed to save telemetry event to file: {str(e)}", exc_info=True)
		else:
			# No session_id provided, can't save to session file
			self._direct_capture(event)
			
	def _direct_capture(self, event: BaseTelemetryEvent) -> None:
		"""
		Directly process a telemetry event without saving to disk.
		
		Args:
			event: The telemetry event to process
		"""
		# Always attempt to forward the event to the feed directory when a
		# session context can be determined.  This guarantees that even in
		# setups where we skip on-disk JSONL persistence, the real-time feed
		# remains comprehensive.
		# Try to extract session_id from event (prefer explicit session_id over agent_id)
		session_id = None
		if hasattr(event, 'session_id') and getattr(event, 'session_id'):
			session_id = getattr(event, 'session_id')
		elif hasattr(event, 'agent_id'):
			session_id = getattr(event, 'agent_id')
			
		if session_id:
			self._save_to_feed_directory(event, session_id)

	def _save_to_feed_directory(self, event: BaseTelemetryEvent, session_id: str) -> None:
		"""
		Save event data to feed directory for later consumption by other components.

		Uses formatter registry to convert events to feed-friendly format.

		Args:
			event: The telemetry event to save
			session_id: The session ID
		"""
		try:
			# ------------------------------------------------------------------
			# Sanitize the provided session identifier.  The caller may pass an
			# *agent* identifier (e.g. "executor_<uuid>") instead of the bare
			# session UUID.  In that case we must first extract the real session
			# ID before feeding it to the PathManager – otherwise a *new* session
			# directory named "executor_<uuid>" (or "planner_<uuid>") would be
			# created on disk which is exactly the bug we are fixing here.
			# ------------------------------------------------------------------
			base_session_id = session_id or ""
			if "_" in base_session_id:
				prefix, remainder = base_session_id.split("_", 1)
				if prefix.lower() in [
					"planner",
					"executor",
					"evaluator",
					"agent",
				]:
					# Use the part after the prefix if it looks like a UUID-like id
					base_session_id = remainder or base_session_id

			# Finally clean it via PathManager to ensure valid characters only
			clean_id = pm().clean_session_id(base_session_id)

			# Now we can safely obtain the feed directory for *the* session id
			feed_dir = pm().get_subdir(clean_id, "feed")

			if not feed_dir:
				self.logger.error(f"Could not create feed directory for session {clean_id}")
				return

			# Use formatter registry to convert event to feed format
			from agents.task.telemetry.formatters import get_formatter_registry
			formatter_registry = get_formatter_registry()
			formatter = formatter_registry.get_formatter(event.name)

			# Format the event
			update_data = formatter.format(event)

			# Save update data to feed directory if we have it
			if update_data:
				# Sanitize update data
				sanitized_update = self._sanitize_telemetry_data(update_data)

				# Enrich with sequence number, millisecond timestamp, and unique ID
				seq_gen = SequenceGenerator.get(clean_id)
				sanitized_update['_seq'] = seq_gen.next()
				sanitized_update['_ts_ms'] = get_timestamp_ms()
				sanitized_update['_id'] = generate_event_id()

				# Generate filename based on sequence (primary) and type
				# Sequence ensures alphabetical sorting = chronological order
				update_type = sanitized_update.get('type', 'update')
				seq = sanitized_update['_seq']

				# Format: {seq:06d}_{type}_{step}.json for guaranteed ordering
				if 'step' in sanitized_update and update_type in ['step', 'planner', 'evaluation']:
					step = sanitized_update.get('step', 0)
					filename = f"{seq:06d}_{update_type}_{step:04d}.json"
				else:
					filename = f"{seq:06d}_{update_type}.json"
				
				# Write update to file with atomic file operations to prevent race conditions
				update_path = feed_dir / filename
				temp_path = update_path.with_suffix('.tmp')
				lock_path = update_path.with_suffix('.lock')
				
				try:
					with SafeFileLock(str(lock_path)):
						try:
							with open(temp_path, 'w') as f:
								json.dump(sanitized_update, f, indent=2, default=self._json_serializable)
							
							# Atomic rename
							temp_path.replace(update_path)

							# Verify file was actually created (safety check)
							if not update_path.exists():
								raise IOError(f"Feed file not created: {update_path}")
							if update_path.stat().st_size == 0:
								raise IOError(f"Feed file is empty: {update_path}")

							self.logger.debug(f"Saved {update_type} update to feed file: {update_path}")

							# Emit to CLI feed callback if registered
							if self.__class__._on_feed_entry:
								try:
									self.__class__._on_feed_entry(clean_id, sanitized_update)
								except Exception:
									pass

							# Direct emit to WebView for low-latency delivery
							# Fire-and-forget after successful file write
							self._emit_to_webview_sync(clean_id, sanitized_update)

							# Enforce feed retention every N writes (throttled so we don't
							# glob the dir on every event). Was dead code before this wiring.
							try:
								cnt = self._feed_write_counts.get(clean_id, 0) + 1
								self._feed_write_counts[clean_id] = cnt
								if cnt % self._feed_retention_every == 0:
									self._enforce_feed_retention(feed_dir)
							except Exception:
								pass
						except Exception as e:
							self.logger.error(f"Failed to write feed file: {e}", exc_info=True)
							if temp_path.exists():
								try:
									temp_path.unlink()  # Clean up failed temp file
								except Exception:
									pass
				except Exception as e:
					self.logger.error(f"Failed to acquire lock for feed file: {e}", exc_info=True)
		
		except Exception as e:
			self.logger.error(f"Error in _save_to_feed_directory: {e}", exc_info=True)
			# Track failure in health stats for visibility
			self._health_stats['failed_captures'] += 1

			# Log at CRITICAL level for production visibility
			self.logger.critical(f"CRITICAL: Feed write failed for {event.name} event in session {clean_id}")

			# Don't raise to prevent breaking agent execution, but make failure visible

	# Note: _detect_service_for_action moved to formatters.py (BaseFeedFormatter)

	def _emit_to_webview_sync(self, session_id: str, event: dict) -> None:
		"""Fire-and-forget HTTP POST to WebView for immediate delivery.

		This enables low-latency event delivery to the WebView UI by
		bypassing the file watcher. The file write is still the source
		of truth - this is an optimization for live updates.

		Args:
			session_id: The session ID (already cleaned)
			event: The enriched event dict to emit
		"""
		try:
			import requests
			requests.post(
				"http://127.0.0.1:5050/api/internal/emit",
				json={"session_id": session_id, "event": event},
				timeout=0.1  # 100ms timeout, non-blocking
			)
		except Exception:
			# Non-critical: file write is source of truth
			# File watcher will still pick up the event
			pass

	def _detect_provider(self, model_name: str) -> str:
		"""Detect the provider based on model name"""
		# Use centralized provider detection
		from agents.task.utils import detect_llm_provider
		provider = detect_llm_provider(None, model_name)

		# Map 'generic' to 'unknown' for consistency with existing code
		return 'unknown' if provider == 'generic' else provider

	def _calculate_cost_from_registry(
		self,
		model_name: str,
		prompt_tokens: Optional[int] = None,
		completion_tokens: Optional[int] = None,
		total_tokens: Optional[int] = None,
		cached_tokens: Optional[int] = None
	) -> Optional[float]:
		"""
		Calculate cost using centralized cost_utils.

		Delegates to modules/credits/cost_utils.py for consistent pricing.

		Args:
			model_name: The name of the model used
			prompt_tokens: Number of prompt tokens (input)
			completion_tokens: Number of completion tokens (output)
			total_tokens: Total tokens (fallback if split not available)
			cached_tokens: Number of cached prompt tokens (for prompt caching)

		Returns:
			Estimated cost in USD or None if cost cannot be estimated
		"""
		from modules.credits.cost_utils import calculate_cost_from_tokens

		if not model_name:
			self.logger.warning("Cannot calculate cost: no model_name provided")
			return None

		try:
			cost = calculate_cost_from_tokens(
				model_name=model_name,
				input_tokens=prompt_tokens,
				output_tokens=completion_tokens,
				total_tokens=total_tokens,
				cached_tokens=cached_tokens or 0
			)
			if cost > 0 and cached_tokens and cached_tokens > 0:
				self.logger.debug(f"Calculated cost for {model_name} with {cached_tokens} cached tokens: ${cost:.6f}")
			return cost if cost > 0 else None
		except Exception as e:
			self.logger.debug(f"Cost calculation failed: {e}")
			return None

	def capture_llm_usage(self, component: str, purpose: str, model_name: str,
						 duration_seconds: float, success: bool,
						 token_count: Optional[int] = None,
						 session_id: Optional[str] = None,
						 prompt_tokens: Optional[int] = None,
						 completion_tokens: Optional[int] = None,
						 cached_tokens: Optional[int] = None,
						 parameters: Optional[Dict[str, Any]] = None,
						 agent_id: Optional[str] = None) -> str:
		"""
		Capture detailed LLM usage statistics with standardized tracking across providers.

		Args:
			component: Component using the LLM (agent, planner, etc.)
			purpose: Purpose of the LLM call (planning, evaluation, etc.)
			model_name: Name of the model used
			duration_seconds: Duration of the LLM call in seconds
			success: Whether the call was successful
			token_count: Total token count (if available)
			session_id: Session ID for tracking
			prompt_tokens: Number of prompt tokens (if available)
			completion_tokens: Number of completion tokens (if available)
			cached_tokens: Number of cached prompt tokens (for prompt caching cost calculation)
			parameters: Dictionary of LLM parameters used (temperature, etc.)
			agent_id: Optional agent ID for explicit tracking

		Returns:
			str: The request_id used for this telemetry entry (for potential reuse)
		"""
		if not self._is_telemetry_enabled():
			return ""
		
		# Track health statistics
		self._health_stats['total_captures'] += 1
		self._health_stats['last_capture_time'] = time.time()
			
		try:
			# Validate required fields for health monitoring
			required_fields = ['component', 'purpose', 'model_name', 'duration_seconds', 'success']
			for field in required_fields:
				field_value = locals().get(field)
				if field_value is None:
					self.logger.warning(f"Missing required field in LLM capture: {field}")
			
			# Check for token data quality
			if token_count is None and prompt_tokens is None and completion_tokens is None:
				self._health_stats['token_estimation_fallbacks'] += 1
				self.logger.warning(f"No token data provided for {model_name} request")
			
			# Clean the session ID
			clean_session_id = None
			if session_id:
				clean_session_id = pm().clean_session_id(session_id)
			
			# If agent_id is provided but session_id is not, try to extract session_id from agent_id
			if agent_id and not clean_session_id:
				if '_' in agent_id:
					parts = agent_id.split('_', 1)
					if len(parts) > 1:
						clean_session_id = pm().clean_session_id(parts[1])
			
			# Use sensible defaults for missing values
			parameters = parameters or {}
			
			# ------------------------------------------------------------------
			# Robust token handling – always derive *total_tokens* when we have
			# the prompt/completion split but the aggregate is missing.  We also
			# normalise string values to integers so the downstream cost
			# estimation as well as the stats aggregation receive consistent
			# numeric data types.
			# ------------------------------------------------------------------
			try:
				def _to_int(val: Any) -> int | None:
					if val is None:
						return None
					if isinstance(val, (int, float)):
						return int(val)
					if isinstance(val, str) and val.isdigit():
						return int(val)
					return None

				token_count      = _to_int(token_count)
				prompt_tokens    = _to_int(prompt_tokens)
				completion_tokens = _to_int(completion_tokens)

				# Derive total when missing but both splits are available
				total_tokens = token_count
				if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
					total_tokens = prompt_tokens + completion_tokens
			except Exception as e:
				self.logger.debug(f"Token normalisation failed: {e}")
				total_tokens = token_count
				
			# Add model provider information using improved detection
			provider = self._detect_provider(model_name)

			# Calculate cost estimate using centralized model registry
			cost_estimate = self._calculate_cost_from_registry(
				model_name=model_name or 'unknown',
				prompt_tokens=prompt_tokens,
				completion_tokens=completion_tokens,
				total_tokens=total_tokens,
				cached_tokens=cached_tokens
			)
				
			# Ensure a unique request ID is attached for downstream de-duplication.
			# If the caller did not provide one we generate a UUID4 hexadecimal token.
			if parameters and isinstance(parameters, dict) and parameters.get('request_id'):
				request_id = parameters.get('request_id')
			else:
				import uuid
				request_id = uuid.uuid4().hex
				if parameters is None:
					parameters = {}
				parameters['request_id'] = request_id

			# Create standardized event data
			event_data = {
				'component': component,
				'purpose': purpose,
				'model_name': model_name or "unknown",
				'provider': provider,
				'duration_seconds': duration_seconds,
				'success': success,
				'token_count': total_tokens,
				'prompt_tokens': prompt_tokens,
				'completion_tokens': completion_tokens,
				# Persist cached prompt tokens so offline cost recompute from the
				# llm_usage JSON is cache-aware (cost_estimate already is). Without
				# this, recompute overstates cost ~2x for cached models (e.g. GLM).
				'cached_tokens': cached_tokens or 0,
				'parameters': parameters,
				'cost_estimate': cost_estimate,
				'agent_id': agent_id,
				'request_id': request_id
			}
			
			# Create the event
			from agents.task.telemetry.views import LLMRequestTelemetryEvent
			
			event = LLMRequestTelemetryEvent(
				component=component,
				purpose=purpose,
				model_name=model_name or "unknown",
				duration_seconds=duration_seconds,
				success=success,
				token_count=total_tokens,
				session_id=clean_session_id,
				error=None if success else "error",  # Differentiate success vs error
				prompt_tokens=prompt_tokens,
				completion_tokens=completion_tokens,
				provider=provider,
				parameters=parameters,
				cost_estimate=cost_estimate,
				agent_id=agent_id,
				request_id=request_id
			)
			
			# FIX FOR DOUBLE COUNTING: Only capture to session-specific LLM usage file
			# Do NOT call self.capture() which creates additional feed entries
			# This eliminates the double counting issue in stats service
			
			# Save to the separate LLM usage feed for cost tracking
			try:
				if clean_session_id:
					# Get LLM usage directory
					# Store LLM usage under data directory for consistency
					data_dir = pm().get_data_dir(clean_session_id)
					llm_usage_dir = data_dir / "llm_usage"
					
					# ------------------------------------------------------------------
					# Use millisecond precision to avoid filename collisions when multiple
					# LLM calls happen within the same second (which is very common for
					# concurrent agent setups).
					# ------------------------------------------------------------------
					timestamp_ms = int(time.time() * 1000)
					filename = f"llm_usage_{timestamp_ms}.json"
					
					# Write to file with atomic operations and locking
					usage_path = llm_usage_dir / filename
					temp_path = usage_path.with_suffix('.tmp')
					lock_path = usage_path.with_suffix('.lock')
					
					# Use filelock for thread safety
					with SafeFileLock(str(lock_path)):
						try:
							# Ensure directory exists
							llm_usage_dir.mkdir(parents=True, exist_ok=True)
							
							# Write to temporary file with proper serialization
							with open(temp_path, 'w') as f:
								json.dump(self._sanitize_telemetry_data(event_data), f, indent=2, default=self._json_serializable)
								
							# Atomic rename operation
							import os
							if os.name == 'nt':  # Windows
								if usage_path.exists():
									usage_path.unlink()
								temp_path.rename(usage_path)
							else:  # Unix
								temp_path.replace(usage_path)
							
							self.logger.debug(f"Saved LLM usage data to {usage_path}")
							
							# Track successful capture
							self._health_stats['successful_llm_captures'] += 1
							
						except Exception as e:
							self.logger.debug(f"Failed to save LLM usage data to file: {e}")
							self._health_stats['failed_llm_captures'] += 1
							if temp_path.exists():
								try:
									temp_path.unlink()  # Clean up
								except:
									pass
									
					# NOTE: LLM data is written ONLY to llm_usage/ directory
					# stats_service.py reads from llm_usage/ as the primary source
					# We no longer write to feed/ to avoid duplicate entries
					# and simplify deduplication in stats aggregation
						
			except Exception as e:
				self.logger.debug(f"Failed to save LLM usage data to file: {e}")
				self._health_stats['failed_llm_captures'] += 1
				
			# Fix 4.2: Return the request_id for potential reuse
			return request_id
			
		except Exception as e:
			self._health_stats['failed_captures'] += 1
			self.logger.debug(f"Failed to capture LLM usage: {e}", exc_info=True)
			return ""

	def get_health_stats(self) -> Dict[str, Any]:
		"""Get telemetry health statistics.
		
		Returns:
			Dictionary containing health metrics and recommendations
		"""
		current_time = time.time()
		uptime_seconds = current_time - self._health_stats['startup_time']
		
		# Calculate rates and percentages
		total_captures = self._health_stats['total_captures']
		failed_captures = self._health_stats['failed_captures']
		
		success_rate = 0.0
		if total_captures > 0:
			success_rate = (total_captures - failed_captures) / total_captures
		
		duplicate_rate = 0.0
		if total_captures > 0:
			duplicate_rate = self._health_stats['duplicate_requests'] / total_captures
		
		token_fallback_rate = 0.0
		if total_captures > 0:
			token_fallback_rate = self._health_stats['token_estimation_fallbacks'] / total_captures
		
		llm_capture_success_rate = 0.0
		total_llm_captures = self._health_stats['successful_llm_captures'] + self._health_stats['failed_llm_captures']
		if total_llm_captures > 0:
			llm_capture_success_rate = self._health_stats['successful_llm_captures'] / total_llm_captures
		
		# Generate health recommendations
		recommendations = []
		if success_rate < 0.95:
			recommendations.append("Low telemetry capture success rate detected. Check LLM client error logs.")
		
		if duplicate_rate > 0.05:
			recommendations.append("High duplicate request rate detected. Check deduplication logic.")
		
		if token_fallback_rate > 0.1:
			recommendations.append("Frequent token estimation fallbacks. Check LLM provider token reporting.")
		
		if llm_capture_success_rate < 0.95 and total_llm_captures > 0:
			recommendations.append("Low LLM file capture success rate. Check file system permissions.")
		
		if self._health_stats['last_capture_time'] and (current_time - self._health_stats['last_capture_time']) > 300:
			recommendations.append("No recent telemetry captures. System may be idle or experiencing issues.")
		
		# Build health status
		health_status = "healthy"
		if success_rate < 0.90 or llm_capture_success_rate < 0.90:
			health_status = "degraded"
		if success_rate < 0.75 or len(recommendations) > 2:
			health_status = "unhealthy"
		
		return {
			'status': health_status,
			'uptime_seconds': uptime_seconds,
			'total_captures': total_captures,
			'failed_captures': failed_captures,
			'success_rate': round(success_rate, 4),
			'duplicate_requests': self._health_stats['duplicate_requests'],
			'duplicate_rate': round(duplicate_rate, 4),
			'token_estimation_fallbacks': self._health_stats['token_estimation_fallbacks'],
			'token_fallback_rate': round(token_fallback_rate, 4),
			'successful_llm_captures': self._health_stats['successful_llm_captures'],
			'failed_llm_captures': self._health_stats['failed_llm_captures'],
			'llm_capture_success_rate': round(llm_capture_success_rate, 4),
			'last_capture_time': self._health_stats['last_capture_time'],
			'recommendations': recommendations,
			'telemetry_enabled': self._is_telemetry_enabled(),
			'posthog_enabled': self.posthog_enabled,
			'feed_writing_enabled': self.feed_writing_enabled
		}

	def capture_step_with_context(
		self, 
		agent_id: str, 
		step: int, 
		actions: List[Dict[str, Any]], 
		task_progress: str, 
		errors: Optional[List[str]] = None, 
		inputs: Optional[Dict[str, Any]] = None, 
		outputs: Optional[Dict[str, Any]] = None, 
		metrics: Optional[Dict[str, Any]] = None,
		agent_name: Optional[str] = None,
		agent_type: Optional[str] = None,
		current_task: Optional[str] = None,
		session_id: Optional[str] = None
	) -> None:
		"""
		Capture an agent step event with enhanced context information.
		
		Args:
			agent_id: The agent's unique identifier
			step: Current step number
			actions: List of actions taken in this step
			task_progress: Brief description of the current task progress
			errors: Optional list of error messages from this step
			inputs: Optional dictionary of input data (state, context, etc.)
			outputs: Optional dictionary of output data (observations, reasoning, etc.)
			metrics: Optional dictionary of metrics for this step
			agent_name: Optional name of the agent (e.g., "planner", "executor")
			agent_type: Optional type of the agent (e.g., "Agent", "PlannerAgent")
			current_task: Optional description of the current task
			session_id: Optional session ID (can be extracted from agent_id)
		"""
		if not self._is_telemetry_enabled():
			return
			
		try:
			# Clean session ID if provided, or extract from agent_id
			clean_session_id = None
			if session_id:
				clean_session_id = pm().clean_session_id(session_id)
			elif agent_id and '_' in agent_id:
				# Extract session ID from agent_id (format: name_session_id)
				parts = agent_id.split('_', 1)
				if len(parts) > 1:
					clean_session_id = pm().clean_session_id(parts[1])
			
			# FIXED: Initialize mutable defaults properly to avoid shared references
			errors = errors or []
			inputs = inputs or {}
			outputs = outputs or {}
			metrics = metrics or {}
			
			# Combine context data into a well-structured object
			context_data = {
				'inputs': inputs,
				'outputs': outputs,
				'metrics': metrics
			}
			
			# Extract reasoning from outputs if available
			reasoning = ""
			if outputs and isinstance(outputs, dict):
				reasoning = outputs.get('reasoning', '')
			
			# Create the telemetry event
			from agents.task.telemetry.views import AgentStepTelemetryEvent
			
			event = AgentStepTelemetryEvent(
				agent_id=agent_id,
				step=step,
				actions=actions,
				step_error=errors,
				consecutive_failures=metrics.get('consecutive_failures', 0) if metrics else 0,
				agent_name=agent_name,
				agent_type=agent_type,
				task_progress=task_progress,
				current_task=current_task,
				reasoning=reasoning,
				context_data=context_data
			)
			
			# Capture the event with session context
			self.capture(event, session_id=clean_session_id)
			
		except Exception as e:
			self.logger.debug(f"Failed to capture agent step with context: {e}", exc_info=True)

	def get_session_id(self) -> Optional[str]:
		"""Get the session ID associated with this event.
		
		Default implementation checks common attributes that might contain the session ID.
		Override this method in subclasses to provide more specific session ID extraction.
		
		Returns:
			The session ID or None if not found
		"""
		# Check for explicit session_id attribute
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			# CRITICAL FIX: Always clean the session ID when retrieving it
			return pm().clean_session_id(getattr(self, 'session_id'))
			
		# Check for parent_session_id attribute (used in relationship events)
		if hasattr(self, 'parent_session_id') and getattr(self, 'parent_session_id'):
			# CRITICAL FIX: Always clean the session ID when retrieving it
			return pm().clean_session_id(getattr(self, 'parent_session_id'))
			
		# Try to extract from agent_id if available
		if hasattr(self, 'agent_id') and getattr(self, 'agent_id'):
			agent_id = getattr(self, 'agent_id')
			if '_' in agent_id:
				# CRITICAL FIX: Always clean the extracted session ID
				session_id = agent_id.split('_', 1)[1]
				return pm().clean_session_id(session_id)
				
		return None

	def _flush_event_buffer(self, session_id: str, force: bool = False) -> None:
		"""Flush buffered events for a session to disk.
		
		Args:
			session_id: Session ID to flush events for
			force: Whether to force flush regardless of buffer size/timeout
		"""
		with self._buffer_lock:
			if session_id not in self._event_buffer:
				return
			
			events = self._event_buffer[session_id]
			if not events:
				return
			
			current_time = time.time()
			last_flush = self._last_flush_times.get(session_id, 0)
			
			# Check if we should flush based on buffer size or timeout
			should_flush = (
				force or 
				len(events) >= self._buffer_size or 
				(current_time - last_flush) >= self._buffer_timeout
			)
			
			if not should_flush:
				return
			
			try:
				# Get telemetry directory
				telemetry_dir = self._ensure_telemetry_dir(session_id)
				if not telemetry_dir:
					self.logger.error(f"Could not create telemetry directory for session {session_id}")
					return
				
				# Write all buffered events to JSONL file in one operation
				telemetry_file = telemetry_dir / "events.jsonl"
				lock_file = telemetry_dir / "events.jsonl.lock"
				
				with SafeFileLock(str(lock_file)):
					with open(telemetry_file, "a") as f:
						for event_data in events:
							f.write(json.dumps(event_data, default=self._json_serializable) + "\n")
				
				# Clear the buffer and update flush time
				self._event_buffer[session_id] = []
				self._last_flush_times[session_id] = current_time
				
				self.logger.debug(f"Flushed {len(events)} telemetry events for session {session_id}")
				
				# Rotate events.jsonl if it exceeds size limits
				try:
					self._rotate_events_file(telemetry_file)
				except Exception as e:
					self.logger.debug(f"Failed to rotate events file: {e}")
			except Exception as e:
				self.logger.error(f"Failed to flush telemetry buffer for session {session_id}: {e}")

	def _add_event_to_buffer(self, session_id: str, event_data: dict) -> None:
		"""Add an event to the buffer for later batch writing.
		
		Args:
			session_id: Session ID to buffer event for
			event_data: Event data to buffer
		"""
		with self._buffer_lock:
			if session_id not in self._event_buffer:
				self._event_buffer[session_id] = []
			
			self._event_buffer[session_id].append(event_data)
			
			# Check if we should flush immediately
			self._flush_event_buffer(session_id)

	def flush_all_buffers(self) -> None:
		"""Flush all buffered events to disk. Useful for cleanup."""
		with self._buffer_lock:
			for session_id in list(self._event_buffer.keys()):
				self._flush_event_buffer(session_id, force=True)

	# ------------------------
	# Retention/rotation utils
	# ------------------------
	def _enforce_feed_retention(self, feed_dir: Path) -> None:
		"""Maintain feed directory size and age limits.

		Keeps only the most recent N files and removes files older than M hours.
		Does not touch summary files like agents.json/services.json/task.json.
		"""
		try:
			max_files = int(os.getenv('TELEMETRY_FEED_MAX_FILES', '200'))
			max_age_hours = int(os.getenv('TELEMETRY_FEED_MAX_AGE_HOURS', '168'))  # 7 days
			now = time.time()

			# Collect candidate files
			protected = {'agents.json', 'services.json', 'task.json'}
			files = [p for p in feed_dir.glob('*.json') if p.name not in protected]
			# Sort newest first
			files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

			# Remove beyond max_files
			for p in files[max_files:]:
				try:
					p.unlink()
				except Exception:
					pass

			# Age-based cleanup
			cutoff = now - max_age_hours * 3600
			for p in files:
				try:
					if p.stat().st_mtime < cutoff:
						p.unlink()
				except Exception:
					pass
		except Exception as e:
			self.logger.debug(f"Retention enforcement error: {e}")

	def _rotate_events_file(self, telemetry_file: Path) -> None:
		"""Rotate events.jsonl when it grows too large."""
		try:
			max_bytes = int(os.getenv('TELEMETRY_EVENTS_MAX_BYTES', '10485760'))  # 10MB
			if telemetry_file.exists() and telemetry_file.stat().st_size > max_bytes:
				ts = time.strftime('%Y%m%d_%H%M%S', time.localtime())
				rotated = telemetry_file.with_name(f"events_{ts}.jsonl")
				lock_file = telemetry_file.with_suffix('.lock')
				with SafeFileLock(str(lock_file)):
					# Rename existing file and create a new empty one
					telemetry_file.rename(rotated)
					telemetry_file.touch()
				self.logger.info(f"Rotated telemetry events file to {rotated}")
		except Exception as e:
			self.logger.debug(f"Events rotation error: {e}")
