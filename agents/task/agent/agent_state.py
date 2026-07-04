
"""
Centralized state management for Task agents.

This module consolidates agent state that was previously scattered across
multiple attributes in the Agent class.

OPTIMIZATION (Nov 14, 2025): Added 199 IQ loop detection fields
"""

import time
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, List


@dataclass
class AgentState:
	"""Centralized agent execution state with intelligent loop detection.

	All agent state should be managed through this class to ensure
	consistency and prevent scattered attributes.
	
	OPTIMIZATION (Nov 14, 2025): Added loop detection capabilities
		- Action similarity tracking
		- Consecutive similar action counter
		- Finding frequency monitoring
		- Stall detection
	"""

	# Step tracking
	n_steps: int = 1
	max_steps: Optional[int] = None
	total_actions_count: int = 0

	# Execution control flags
	paused: bool = False
	stopped: bool = False
	done: bool = False

	# Error tracking
	consecutive_failures: int = 0
	total_failures: int = 0
	max_failures: int = 3

	# History and results
	last_result: Optional[List[Any]] = None
	last_action: Optional[str] = None
	last_model_output: Optional[Any] = None

	# Timing
	session_created_at: Optional[datetime] = None
	last_activity_time: float = field(default_factory=time.time)
	last_step_start_time: float = field(default_factory=time.time)

	# Session metadata
	session_continuation_context: Optional[str] = None
	
	# OPTIMIZATION: Loop detection fields (Nov 14, 2025)
	recent_action_texts: List[str] = field(default_factory=list)  # Last 10 action summaries
	consecutive_similar_actions: int = 0  # Counter for similar actions
	last_finding_step: int = 0  # Step number when last finding was added
	findings_last_n_steps: int = 0  # Count of findings in last N steps
	loop_warning_count: int = 0  # How many loop warnings issued
	max_loop_warnings: int = 3  # Trigger intervention after N warnings

	# LLM Error Tracking (Dec 2025) - Circuit breaker pattern for provider failures
	llm_error_type_counts: dict = field(default_factory=dict)  # Error type → count
	llm_providers_failed: List[str] = field(default_factory=list)  # Providers that have failed
	last_llm_error_type: Optional[str] = None  # Most recent error type
	llm_circuit_breaker_open: bool = False  # True if circuit breaker tripped
	CIRCUIT_BREAKER_THRESHOLD: int = 3  # Same error type N times → circuit breaker

	def reset_failures(self) -> None:
		"""Reset consecutive failure count (called after successful step)."""
		self.consecutive_failures = 0

	def increment_failures(self) -> None:
		"""Increment failure counts."""
		self.consecutive_failures += 1
		self.total_failures += 1

	def is_too_many_failures(self) -> bool:
		"""Check if consecutive failures exceed maximum."""
		return self.consecutive_failures >= self.max_failures

	def increment_step(self) -> None:
		"""Increment step counter."""
		self.n_steps += 1

	def increment_actions(self, count: int = 1) -> None:
		"""Increment total actions count."""
		self.total_actions_count += count

	def update_activity_time(self) -> None:
		"""Update last activity timestamp."""
		self.last_activity_time = time.time()

	def should_stop(self) -> bool:
		"""Check if agent should stop execution."""
		return self.stopped or self.done
	
	# OPTIMIZATION: Loop detection methods (Nov 14, 2025)
	
	def track_action(self, action_summary: str) -> None:
		"""Track action for loop detection.
		
		Args:
			action_summary: Summary of the action taken
		"""
		# Keep last 10 actions
		self.recent_action_texts.append(action_summary)
		if len(self.recent_action_texts) > 10:
			self.recent_action_texts.pop(0)
		
		# Check similarity with previous action
		if len(self.recent_action_texts) >= 2:
			current = action_summary.lower()
			previous = self.recent_action_texts[-2].lower()
			
			# Simple similarity: check for common words
			current_words = set(current.split())
			previous_words = set(previous.split())
			
			if len(current_words) > 0 and len(previous_words) > 0:
				overlap = len(current_words & previous_words)
				similarity = overlap / max(len(current_words), len(previous_words))
				
				if similarity > 0.7:  # 70% word overlap = similar action
					self.consecutive_similar_actions += 1
				else:
					self.consecutive_similar_actions = 0
		
	def track_finding(self) -> None:
		"""Track that a finding was added (resets stall detection)."""
		self.last_finding_step = self.n_steps
		self.findings_last_n_steps += 1
		self.consecutive_similar_actions = 0  # Reset on progress
	
	def is_stuck_in_loop(self) -> bool:
		"""Check if agent is stuck in infinite loop.
		
		Multi-signal detection:
		1. Same action repeated 5+ times
		2. No findings in 15+ steps
		3. Multiple loop warnings already issued
		
		Returns:
			True if loop detected, False otherwise
		"""
		# Signal 1: Repetitive actions
		if self.consecutive_similar_actions >= 5:
			return True
		
		# Signal 2: No progress (findings)
		steps_since_finding = self.n_steps - self.last_finding_step
		if steps_since_finding > 15:
			return True
		
		# Signal 3: Too many warnings (agent not responding to guidance)
		if self.loop_warning_count >= self.max_loop_warnings:
			return True
		
		return False
	
	def is_showing_loop_symptoms(self) -> bool:
		"""Check for early loop symptoms (warning signs).
		
		Returns:
			True if showing symptoms, False otherwise
		"""
		# Symptom 1: Moderate repetition
		if self.consecutive_similar_actions >= 3:
			return True
		
		# Symptom 2: Slow progress
		steps_since_finding = self.n_steps - self.last_finding_step
		if steps_since_finding > 8:
			return True
		
		return False
	
	def reset_loop_detection(self) -> None:
		"""Reset loop detection counters (after successful intervention)."""
		self.consecutive_similar_actions = 0
		self.loop_warning_count = 0

	# LLM Error Tracking methods (Dec 2025)

	def track_llm_error(self, error_type: str, provider: str) -> bool:
		"""Track an LLM error for circuit breaker pattern.

		Args:
			error_type: Type of error (e.g., 'LLMRateLimitError')
			provider: Provider that failed

		Returns:
			True if circuit breaker should trip (same error 3+ times)
		"""
		# Track error type count
		self.llm_error_type_counts[error_type] = self.llm_error_type_counts.get(error_type, 0) + 1
		self.last_llm_error_type = error_type

		# Track failed provider
		if provider and provider not in self.llm_providers_failed:
			self.llm_providers_failed.append(provider)

		# Check circuit breaker threshold
		if self.llm_error_type_counts[error_type] >= self.CIRCUIT_BREAKER_THRESHOLD:
			self.llm_circuit_breaker_open = True
			return True

		return False

	def reset_llm_errors(self, reset_circuit_breaker: bool = False) -> None:
		"""Reset LLM error tracking (after successful fallback).

		Args:
			reset_circuit_breaker: Also reset circuit breaker state
		"""
		self.llm_error_type_counts.clear()
		self.last_llm_error_type = None
		if reset_circuit_breaker:
			self.llm_circuit_breaker_open = False
			self.llm_providers_failed.clear()

	def should_halt_for_llm_error(self) -> bool:
		"""Check if session should halt due to LLM errors.

		Returns:
			True if circuit breaker is open or too many provider failures
		"""
		return self.llm_circuit_breaker_open or len(self.llm_providers_failed) >= 3

	def is_at_max_steps(self) -> bool:
		"""Check if maximum steps reached."""
		if self.max_steps is None:
			return False
		return self.n_steps >= self.max_steps

	# PERSISTENCE: Session state save/load for app restart recovery

	def to_dict(self) -> dict:
		"""Serialize agent state to dictionary for persistence.

		Returns:
			Dictionary representation of agent state
		"""
		return {
			'version': '1.1',  # Updated for LLM error tracking
			'n_steps': self.n_steps,
			'max_steps': self.max_steps,
			'total_actions_count': self.total_actions_count,
			'paused': self.paused,
			'stopped': self.stopped,
			'done': self.done,
			'consecutive_failures': self.consecutive_failures,
			'total_failures': self.total_failures,
			'max_failures': self.max_failures,
			'last_action': self.last_action,
			'session_continuation_context': self.session_continuation_context,
			# Loop detection state
			'recent_action_texts': self.recent_action_texts,
			'consecutive_similar_actions': self.consecutive_similar_actions,
			'last_finding_step': self.last_finding_step,
			'findings_last_n_steps': self.findings_last_n_steps,
			'loop_warning_count': self.loop_warning_count,
			'max_loop_warnings': self.max_loop_warnings,
			# LLM error tracking (Dec 2025)
			'llm_error_type_counts': self.llm_error_type_counts,
			'llm_providers_failed': self.llm_providers_failed,
			'last_llm_error_type': self.last_llm_error_type,
			'llm_circuit_breaker_open': self.llm_circuit_breaker_open,
			# Timestamps
			'session_created_at': self.session_created_at.isoformat() if self.session_created_at else None,
			'last_activity_time': self.last_activity_time,
			'last_step_start_time': self.last_step_start_time
		}

	@classmethod
	def from_dict(cls, data: dict) -> 'AgentState':
		"""Deserialize agent state from dictionary.

		Args:
			data: Dictionary representation of agent state

		Returns:
			Restored AgentState instance

		Raises:
			ValueError: If version mismatch or invalid data
		"""
		# Validate version (support 1.0 and 1.1)
		version = data.get('version')
		if version not in ('1.0', '1.1'):
			raise ValueError(f"Unsupported agent state version: {version}")

		# Create instance with restored data
		state = cls(
			n_steps=data.get('n_steps', 1),
			max_steps=data.get('max_steps'),
			total_actions_count=data.get('total_actions_count', 0),
			paused=data.get('paused', False),
			stopped=data.get('stopped', False),
			done=data.get('done', False),
			consecutive_failures=data.get('consecutive_failures', 0),
			total_failures=data.get('total_failures', 0),
			max_failures=data.get('max_failures', 3),
			last_action=data.get('last_action'),
			session_continuation_context=data.get('session_continuation_context'),
			# Loop detection
			recent_action_texts=data.get('recent_action_texts', []),
			consecutive_similar_actions=data.get('consecutive_similar_actions', 0),
			last_finding_step=data.get('last_finding_step', 0),
			findings_last_n_steps=data.get('findings_last_n_steps', 0),
			loop_warning_count=data.get('loop_warning_count', 0),
			max_loop_warnings=data.get('max_loop_warnings', 3),
			# LLM error tracking (Dec 2025) - defaults for v1.0 compatibility
			llm_error_type_counts=data.get('llm_error_type_counts', {}),
			llm_providers_failed=data.get('llm_providers_failed', []),
			last_llm_error_type=data.get('last_llm_error_type'),
			llm_circuit_breaker_open=data.get('llm_circuit_breaker_open', False),
			# Timestamps
			session_created_at=datetime.fromisoformat(data['session_created_at']) if data.get('session_created_at') else None,
			last_activity_time=data.get('last_activity_time', time.time()),
			last_step_start_time=data.get('last_step_start_time', time.time())
		)
		return state

	def save_to_file(self, filepath: Path) -> bool:
		"""Save agent state to JSON file with atomic write.

		Args:
			filepath: Path to save state file

		Returns:
			True if save successful, False otherwise
		"""
		try:
			filepath = Path(filepath)
			filepath.parent.mkdir(parents=True, exist_ok=True)

			# Atomic write using temp file + rename
			temp_fd, temp_path = tempfile.mkstemp(
				dir=filepath.parent,
				prefix='.agent_state_',
				suffix='.tmp'
			)

			try:
				with os.fdopen(temp_fd, 'w') as f:
					json.dump(self.to_dict(), f, indent=2)
					f.flush()
					os.fsync(f.fileno())

				# Atomic rename
				os.replace(temp_path, str(filepath))
				return True
			except Exception:
				# Clean up temp file on error
				if os.path.exists(temp_path):
					os.unlink(temp_path)
				raise

		except Exception as e:
			# Log error but don't crash agent
			import logging
			logging.getLogger(__name__).error(f"Failed to save agent state: {e}")
			return False

	@classmethod
	def load_from_file(cls, filepath: Path) -> Optional['AgentState']:
		"""Load agent state from JSON file.

		Args:
			filepath: Path to state file

		Returns:
			Restored AgentState instance, or None if file doesn't exist or invalid
		"""
		try:
			filepath = Path(filepath)
			if not filepath.exists():
				return None

			with open(filepath, 'r') as f:
				data = json.load(f)

			return cls.from_dict(data)
		except Exception as e:
			# Log error but don't crash agent
			import logging
			logging.getLogger(__name__).error(f"Failed to load agent state: {e}")
			return None

	def __repr__(self) -> str:
		"""Return string representation for debugging."""
		return (
			f"AgentState("
			f"steps={self.n_steps}/{self.max_steps or '∞'}, "
			f"actions={self.total_actions_count}, "
			f"failures={self.consecutive_failures}/{self.max_failures}, "
			f"paused={self.paused}, stopped={self.stopped}, done={self.done})"
		)
