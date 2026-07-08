from typing import Optional, List, Literal
from pathlib import Path
import urllib.parse

from pydantic import BaseModel, model_validator, Field, field_validator, ConfigDict


# Action Input Models
class DoneAction(BaseModel):
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	text: str = Field(default="", alias='message')

	@field_validator('text')
	@classmethod
	def normalize_text(cls, v):
		return str(v) if v is not None else ""


class SendMessageAction(BaseModel):
	"""Send a message to the user."""
	model_config = ConfigDict(extra='forbid')
	
	text: str = Field(description="Message to send to user")
	wait_for_response: bool = Field(
		default=False,
		description="If True, pause and wait for user to respond"
	)
	timeout_seconds: int = Field(
		default=300,
		description="How long to wait for response (if wait_for_response=True)"
	)


class MessageTargetAction(BaseModel):
	"""Send a message to a specific chat/recipient on a specific surface.
	Only owner + owner-allowlisted targets are permitted (default-deny)."""
	model_config = ConfigDict(extra='forbid')

	surface: str = Field(description="Surface id, e.g. 'telegram', 'email', 'whatsapp'")
	target: str = Field(description="Recipient/chat id on that surface (chat id, @user, or email address)")
	text: str = Field(description="Message body to send")
	action: str = Field(default="send", description="send | reply | edit | delete | react")
	reply_to: Optional[str] = Field(default=None, description="Message id to reply to")
	message_id: Optional[str] = Field(default=None, description="Target message id for edit/delete/react")


# Twitter specific actions
class TwitterSearchAction(BaseModel):
	"""Model for searching tweets"""
	query: str
	max_results: Optional[int] = 10
	include_replies: Optional[bool] = False


class TwitterGetUserAction(BaseModel):
	"""Model for getting Twitter user information"""
	username: str


class TwitterGetTweetsAction(BaseModel):
	"""Model for getting tweets from a user"""
	user_id: str
	max_results: Optional[int] = 10


# Web fetch (stateless page reader)
class WebFetchAction(BaseModel):
	"""Model for fetching and reading a single web page as markdown."""
	url: str
	max_chars: int = 40000


# Perplexity specific actions
class PerplexitySearchAction(BaseModel):
	"""Model for searching using Perplexity AI"""
	query: str


class PerplexityAnalyzeAction(BaseModel):
	"""Model for getting detailed analysis on a topic"""
	topic: str


class PerplexitySourcesAction(BaseModel):
	"""Model for getting trusted sources on a topic"""
	topic: str


# Email specific actions
class EmailSendAction(BaseModel):
	"""Model for sending an email"""
	model_config = ConfigDict(extra='forbid')

	to_email: str = Field(description="Recipient email address")
	subject: str = Field(description="Email subject line")
	body: str = Field(description="Plain text email body")
	html: Optional[str] = Field(default=None, description="HTML email body (optional)")
	cc: Optional[str] = Field(default=None, description="CC recipients (comma-separated)")
	bcc: Optional[str] = Field(default=None, description="BCC recipients (comma-separated)")

	@field_validator('to_email', 'cc', 'bcc')
	@classmethod
	def validate_email_format(cls, v, info):
		"""Validate email addresses have basic format."""
		if v is None:
			return v
		# Split by comma for cc/bcc
		emails = [e.strip() for e in v.split(',') if e.strip()]
		for email in emails:
			if '@' not in email or '.' not in email.split('@')[-1]:
				raise ValueError(f"Invalid email format: {email}")
		return v

	@field_validator('subject', 'body')
	@classmethod
	def validate_not_empty(cls, v, info):
		"""Ensure subject and body are not empty."""
		if not v or not v.strip():
			raise ValueError(f"{info.field_name} cannot be empty")
		return v


class EmailReadAction(BaseModel):
	"""Model for reading emails"""
	folder: Optional[str] = "INBOX"
	limit: Optional[int] = 10
	unread_only: Optional[bool] = False


# FileSystem specific actions
class DocProcessAction(BaseModel):
	"""Model for processing a document"""
	content: str
	enhance: Optional[bool] = False


class DocAnalyzeAction(BaseModel):
	"""Model for analyzing a document"""
	text: str
	analysis_type: Optional[str] = "general"


class DocProcessUrlAction(BaseModel):
	"""Model for processing content from a URL"""
	url: str


# File operation actions
class ReadFileAction(BaseModel):
	"""Model for reading a file from the workspace"""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	file_path: str = Field(alias='filePath')
	offset: Optional[int] = Field(default=None, description="Starting line number (1-indexed, inclusive)")
	limit: Optional[int] = Field(default=None, description="Number of lines to read from offset")
	# Character-based chunking for JSON/dense files where line-based chunking doesn't help
	char_offset: Optional[int] = Field(default=None, description="Starting character position (0-indexed)")
	char_limit: Optional[int] = Field(default=None, description="Number of characters to read from char_offset")

	@field_validator('file_path')
	@classmethod
	def normalize_path(cls, v):
		if isinstance(v, str):
			return str(Path(v.strip()))
		return v


class WriteFileAction(BaseModel):
	"""Model for writing content to a file in the workspace"""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	file_path: str = Field(alias='filePath')
	content: str

	@field_validator('file_path')
	@classmethod
	def normalize_path(cls, v):
		if isinstance(v, str):
			return str(Path(v.strip()))
		return v

	@field_validator('content')
	@classmethod
	def normalize_content(cls, v):
		return str(v) if v is not None else ""


class AppendFileAction(BaseModel):
	"""Model for appending content to a file in the workspace"""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	file_path: str = Field(alias='filePath')
	content: str

	@field_validator('file_path')
	@classmethod
	def normalize_path(cls, v):
		if isinstance(v, str):
			return str(Path(v.strip()))
		return v

	@field_validator('content')
	@classmethod
	def normalize_content(cls, v):
		return str(v) if v is not None else ""


class ListDirectoryAction(BaseModel):
	"""Model for listing files in a directory"""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	directory: Optional[str] = Field(default=".", alias='directoryPath')

	@field_validator('directory')
	@classmethod
	def normalize_directory(cls, v):
		if v is None:
			return "."
		if isinstance(v, str):
			return str(Path(v.strip())) if v.strip() else "."
		return v


class DeleteFileAction(BaseModel):
	"""Model for deleting a file"""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	file_path: str = Field(alias='filePath')

	@field_validator('file_path')
	@classmethod
	def normalize_path(cls, v):
		if isinstance(v, str):
			return str(Path(v.strip()))
		return v


class CreateDirectoryAction(BaseModel):
	"""Model for creating a directory"""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	directory_path: str = Field(alias='directoryPath')

	@field_validator('directory_path')
	@classmethod
	def normalize_path(cls, v):
		if isinstance(v, str):
			return str(Path(v.strip()))
		return v


# Todo Management Actions
class TodoListAction(BaseModel):
	"""List all todos in the current session"""
	model_config = ConfigDict(extra='ignore')  # Allow extra fields but ignore them


class TodoAddAction(BaseModel):
	"""Add a new todo item"""
	model_config = ConfigDict(extra='forbid')

	text: str = Field(description="The todo item text")
	parent_pattern: Optional[str] = Field(default=None, description="Optional parent todo to nest under")

	@field_validator('text')
	@classmethod
	def validate_text_not_empty(cls, v):
		"""Ensure todo text is not empty."""
		if not v or not v.strip():
			raise ValueError("Todo text cannot be empty")
		return v.strip()


class TodoCompleteAction(BaseModel):
	"""Mark a todo item as complete by ID or text pattern"""
	model_config = ConfigDict(extra='forbid')

	pattern: str = Field(description="Todo ID (e.g., '1') or text pattern to match (e.g., 'Navigate to OpenAI')")

	@field_validator('pattern')
	@classmethod
	def validate_pattern_not_empty(cls, v):
		"""Ensure pattern is not empty."""
		if not v or not v.strip():
			raise ValueError("Pattern cannot be empty - provide todo ID or text")
		return v.strip()


class TodoProgressAction(BaseModel):
	"""Get current todo progress"""
	model_config = ConfigDict(extra='ignore')  # Allow extra fields but ignore them


class TodoNextAction(BaseModel):
	"""Get the next incomplete task"""
	model_config = ConfigDict(extra='ignore')  # Allow extra fields but ignore them


# Sub-Agent / Subtask Actions
class SubtaskAction(BaseModel):
	"""Delegate a complex, independent subtask to a sub-agent.

	⚠️ WARNING: Sub-agents are EXPENSIVE (10-60+ API calls each).

	ONLY use when ALL conditions are met:
	- Task is truly independent (no shared state needed)
	- Task requires 10+ steps of focused work
	- Task description is detailed (>80 characters)
	- Time savings outweigh context loss

	DO NOT use for simple tasks like:
	- File operations (read, write, save)
	- Single API calls or searches
	- Tasks completable in <5 steps

	The sub-agent runs to completion and returns structured output including
	extracted data, files created, and usage statistics.
	"""
	model_config = ConfigDict(extra='forbid')

	task: str = Field(
		...,
		description="Detailed subtask description (>80 chars). Include all context the sub-agent needs.",
		min_length=20
	)
	profile: str = Field(
		default="executor",
		description="Sub-agent profile (only 'executor' is implemented; other values fall back to it)"
	)
	max_steps: int = Field(
		default=30,
		description="Maximum steps for sub-agent (default 30, max 50)",
		le=50
	)
	model: Optional[str] = Field(
		default=None,
		description="Optional model for this sub-agent (e.g. 'claude-haiku-4-5' or an "
		            "OpenRouter slug). Omit to inherit the parent's model (or the "
		            "delegate_task-level model when used inside 'tasks')."
	)
	provider: Optional[str] = Field(
		default=None,
		description="Optional provider for THIS task's 'model'; omit to auto-detect from "
		            "the model name. Requires 'model' — a provider set on a task without "
		            "its own 'model' is rejected (the top-level 'model' is used, so a "
		            "task-level provider without a task-level model is discarded)."
	)


class ParallelSubtasksAction(BaseModel):
	"""Run 2-4 independent subtasks in parallel using sub-agents.

	⚠️ WARNING: Very expensive - spawns multiple full agent conversations.

	ONLY use when you have genuinely independent work that can run simultaneously.
	Each subtask should be complex enough to justify sub-agent overhead.

	All subtasks run in parallel and results are collected with usage statistics.
	"""
	model_config = ConfigDict(extra='forbid')

	subtasks: List[SubtaskAction] = Field(
		...,
		description="List of independent subtasks to run in parallel (2-5 tasks)",
		min_length=2,
		max_length=5
	)


class DelegateTaskAction(BaseModel):
	"""Delegate work to one or more sub-agents (the unified Reference-style surface).

	Provide EXACTLY ONE of:
	- ``goal``: a single focused goal handed to one sub-agent, or
	- ``tasks``: 2-5 independent subtasks run in parallel.

	⚠️ Sub-agents are EXPENSIVE (10-60+ API calls each). Only delegate genuinely
	independent, multi-step work.

	By default the call is SYNCHRONOUS — it blocks the current turn until the
	child finishes. Set ``background=true`` (goal shape only) to dispatch the child
	detached: you get a delegation_id immediately and keep working; the result
	arrives as a new message when it finishes. Background delegation is durable
	ACROSS THE TURN but NOT across a process restart — for long-running or scheduled
	durable work use the scheduler (cron), not background delegation.

	``role`` sets the role of the spawned sub-agent(s): 'leaf' (default) cannot
	delegate further; 'orchestrator' may, subject to the depth limit.
	"""
	model_config = ConfigDict(extra='forbid')

	goal: Optional[str] = Field(
		default=None,
		description="Single delegated goal (>20 chars). Use this OR 'tasks'.",
		min_length=20,
	)
	tasks: Optional[List[SubtaskAction]] = Field(
		default=None,
		description="2-5 independent subtasks to run in parallel. Use this OR 'goal'.",
		min_length=2,
		max_length=5,
	)
	profile: str = Field(
		default="executor",
		description="Sub-agent profile when 'goal' is used (only 'executor' is implemented; other values fall back to it)",
	)
	max_steps: int = Field(
		default=30,
		description="Maximum steps for the sub-agent when 'goal' is used (default 30, max 50)",
		le=50,
	)
	role: Literal["leaf", "orchestrator"] = Field(
		default="leaf",
		description="Role for spawned sub-agent(s): 'leaf' cannot delegate further; 'orchestrator' may (subject to depth limits).",
	)
	background: bool = Field(
		default=False,
		description=(
			"If true, dispatch a single 'goal' in the BACKGROUND: return immediately with "
			"a delegation_id and keep working; the result arrives as a new message when the "
			"task finishes. Durable across the turn (NOT across restart — use the scheduler "
			"for that). Only valid with 'goal' (not 'tasks'). NOT supported together with "
			"'model' in v1 (background delegation always inherits the parent's model)."
		),
	)
	model: Optional[str] = Field(
		default=None,
		description="Optional model for the sub-agent (e.g. 'claude-haiku-4-5' or an "
		            "OpenRouter slug). Omit to inherit the parent's model. With 'tasks', "
		            "this is the fallback for any task that doesn't set its own 'model'. "
		            "Not supported with background=true."
	)
	provider: Optional[str] = Field(
		default=None,
		description="Optional provider for the top-level 'model'; omit to auto-detect "
		            "from the model name. Requires 'model' — a provider without a model "
		            "is rejected (it would otherwise be silently discarded)."
	)

	@model_validator(mode="after")
	def _exactly_one_shape(self):
		if bool(self.goal) == bool(self.tasks):
			raise ValueError("Provide exactly one of 'goal' or 'tasks'.")
		# UP-12: background mode supports only the single 'goal' shape in v1.
		if self.background and not self.goal:
			raise ValueError("background=true is only supported with the 'goal' shape (not 'tasks').")
		return self


