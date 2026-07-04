"""Native message types for LLM conversations.

This module provides POLYROB's native message types (no third-party agent-framework dependency).
These are POLYROB's native message types.

Usage:
    from modules.llm.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage, BaseMessage

    # Create messages in the standard chat format
    sys_msg = SystemMessage(content="You are a helpful assistant")
    user_msg = HumanMessage(content="Hello!")
    ai_msg = AIMessage(content="Hi there!", tool_calls=[...])
    tool_msg = ToolMessage(content="result", tool_call_id="call_123")

    # isinstance checks work as expected
    isinstance(ai_msg, AIMessage)  # True
    isinstance(ai_msg, BaseMessage)  # True

MIGRATION NOTE:
Import message types from `modules.llm.messages`.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union


class MessageOrigin:
    """Where a message *came from*, independent of its wire role.

    Providers only have system/user/assistant/tool roles, so control content the
    agent injects (loop interventions, approval results, hierarchical memory, etc.)
    has historically been crammed into a plain ``HumanMessage`` — indistinguishable
    from a genuine user turn. ``origin`` is in-process metadata (never sent on the
    wire) that records the true source; ``make_control_message`` additionally
    envelopes the content so the model can tell it apart from real user input.
    """

    USER = "user"                 # a genuine human turn
    GUIDANCE = "guidance"         # mid-task system guidance
    INTERVENTION = "intervention" # loop / stall / contract-violation correction
    APPROVAL = "approval"         # result of a human-in-the-loop approval decision
    MEMORY = "memory"             # injected hierarchical/session memory (in-session H-MEM)
    RECALL = "recall"             # cross-session recall from past sessions (distinct from in-session MEMORY)
    SKILL = "skill"               # injected skills (kept out of the system prompt)
    SELF_CONTEXT = "self_context"       # frozen SOUL/identity doc pinned in the foundation
    PROJECT_CONTEXT = "project_context" # auto-loaded CLAUDE.md/AGENTS.md for CLI mode
    SYSTEM_NOTE = "system_note"         # other system-generated notice
    TOOL_NOTICE = "tool_notice"         # tool deferral / throttling notice
    CORRESPONDENT = "correspondent"     # WS-A: reply from a third party the agent
                                        # contacted — DATA, never an instruction
    COMPACTION_SUMMARY = "compaction_summary"  # lossy LLM summary of compacted history (derived, not a user turn)
    SELF_WAKE = "self_wake"             # forged re-entry turn (self-wake / autonomy rail),
                                        # NOT a genuine human message
    RUNTIME_IDENTITY = "runtime_identity"  # the model/provider the agent is actually
                                        # running on — so it can answer truthfully
    EPISODIC_DIGEST = "episodic_digest"    # session-start recent-activity digest
                                        # (own recent runs), chat/owner sessions only
    SESSION_BRIDGE = "session_bridge"      # cross-session continuity bridge (Task 6)


# Origin -> XML-ish envelope tag used to wrap injected (non-user) content so the
# model reads it as a distinct block rather than a user message.
_ORIGIN_ENVELOPE = {
    MessageOrigin.GUIDANCE: "system-directive",
    MessageOrigin.INTERVENTION: "system-directive",
    MessageOrigin.APPROVAL: "approval-result",
    MessageOrigin.MEMORY: "session-memory",
    MessageOrigin.RECALL: "recalled-from-past-sessions",
    MessageOrigin.SKILL: "available-skills",
    MessageOrigin.SELF_CONTEXT: "self-context",
    MessageOrigin.PROJECT_CONTEXT: "project-context",
    MessageOrigin.SYSTEM_NOTE: "system-note",
    MessageOrigin.TOOL_NOTICE: "tool-notice",
    MessageOrigin.CORRESPONDENT: "correspondent-message",
    MessageOrigin.COMPACTION_SUMMARY: "compacted-history",
    MessageOrigin.SELF_WAKE: "self-wake",
    MessageOrigin.RUNTIME_IDENTITY: "runtime-identity",
}


@dataclass
class BaseMessage:
    """Base class for all message types.

    Attributes:
        content: The message content (string or list for multimodal)
        additional_kwargs: Extra metadata (e.g., tool_calls for AI messages)
        type: Message type identifier (system, human, ai, tool)
        name: Optional name for the message sender
        id: Optional unique identifier for the message
        origin: True source of the message (see MessageOrigin). In-process only;
            never serialized to the provider wire format.
    """
    content: Union[str, List[Dict[str, Any]]]
    additional_kwargs: Dict[str, Any] = field(default_factory=dict)
    type: str = "base"
    name: Optional[str] = None
    id: Optional[str] = None
    origin: str = MessageOrigin.USER

    def __post_init__(self):
        """Ensure additional_kwargs is always a dict."""
        if self.additional_kwargs is None:
            self.additional_kwargs = {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert message to the provider API wire shape (role/content/name).

        D3: this is intentionally NARROWER than the durable serialization —
        ``origin``/``metadata`` are deliberately omitted because this dict is sent
        to LLM providers, which reject unknown keys. The round-trippable durable
        path that DOES preserve origin + metadata is ``save_to_disk`` /
        ``load_from_disk`` (message_manager persistence). Do not assume
        ``to_dict()`` round-trips those fields.
        """
        result = {
            "role": self._get_role(),
            "content": self.content,
        }
        if self.name:
            result["name"] = self.name
        return result

    def _get_role(self) -> str:
        """Get the role string for API calls."""
        role_map = {
            "system": "system",
            "human": "user",
            "ai": "assistant",
            "tool": "tool",
        }
        return role_map.get(self.type, "user")

    def __repr__(self) -> str:
        """Return a string representation of the message."""
        content_preview = self.content[:50] if isinstance(self.content, str) else str(self.content)[:50]
        return f"{self.__class__.__name__}(content='{content_preview}...')"


@dataclass
class SystemMessage(BaseMessage):
    """System message that sets context/instructions for the conversation.

    Example:
        msg = SystemMessage(content="You are a helpful coding assistant")
    """
    content: Union[str, List[Dict[str, Any]]] = ""
    additional_kwargs: Dict[str, Any] = field(default_factory=dict)
    type: str = field(default="system", init=False)
    name: Optional[str] = None
    id: Optional[str] = None


@dataclass
class HumanMessage(BaseMessage):
    """Message from a human user.

    Example:
        msg = HumanMessage(content="Help me write a function")

        # Multimodal with image
        msg = HumanMessage(content=[
            {"type": "text", "text": "What's in this image?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        ])
    """
    content: Union[str, List[Dict[str, Any]]] = ""
    additional_kwargs: Dict[str, Any] = field(default_factory=dict)
    type: str = field(default="human", init=False)
    name: Optional[str] = None
    id: Optional[str] = None


@dataclass
class AIMessage(BaseMessage):
    """Message from an AI assistant.

    Attributes:
        content: The text response from the AI
        tool_calls: List of tool calls made by the AI (native function calling)
        usage_metadata: Token usage information from the API response

    Example:
        # Simple response
        msg = AIMessage(content="Here's the function...")

        # Response with tool calls
        msg = AIMessage(
            content="",
            tool_calls=[{
                "id": "call_123",
                "name": "search",
                "args": {"query": "python"}
            }]
        )
    """
    content: Union[str, List[Dict[str, Any]]] = ""
    additional_kwargs: Dict[str, Any] = field(default_factory=dict)
    type: str = field(default="ai", init=False)
    name: Optional[str] = None
    id: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage_metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        """Initialize defaults and sync tool_calls with additional_kwargs."""
        super().__post_init__()

        # Ensure tool_calls is a list
        if self.tool_calls is None:
            self.tool_calls = []

        # Sync tool_calls with additional_kwargs for backward compatibility
        if self.tool_calls and "tool_calls" not in self.additional_kwargs:
            self.additional_kwargs["tool_calls"] = self.tool_calls

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict format for API calls."""
        result = super().to_dict()
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        return result


@dataclass
class ToolMessage(BaseMessage):
    """Result message from a tool execution.

    Attributes:
        content: The result/output from the tool
        tool_call_id: ID of the tool call this is responding to

    Example:
        msg = ToolMessage(
            content='{"result": "success", "data": [...]}',
            tool_call_id="call_123"
        )
    """
    content: Union[str, List[Dict[str, Any]]] = ""
    tool_call_id: str = ""
    additional_kwargs: Dict[str, Any] = field(default_factory=dict)
    type: str = field(default="tool", init=False)
    name: Optional[str] = None
    id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict format for API calls."""
        result = super().to_dict()
        result["tool_call_id"] = self.tool_call_id
        return result


# Type alias for any message type (the native BaseMessage union)
MessageType = Union[SystemMessage, HumanMessage, AIMessage, ToolMessage, BaseMessage]


@dataclass
class ChatGeneration:
    """A single generation result from an LLM.

    This wraps a message with optional metadata about the generation.

    Attributes:
        message: The generated message
        generation_info: Optional metadata (e.g., raw API response)
        text: Legacy attribute for backward compatibility (returns message.content)
    """
    message: BaseMessage
    generation_info: Optional[Dict[str, Any]] = None

    @property
    def text(self) -> str:
        """Return the text content of the message (legacy compatibility)."""
        if isinstance(self.message.content, str):
            return self.message.content
        return str(self.message.content)


@dataclass
class ChatResult:
    """Result from an LLM chat completion.

    Attributes:
        generations: List of generated responses
        llm_output: Optional metadata from the LLM (e.g., token usage)
    """
    generations: List[ChatGeneration] = field(default_factory=list)
    llm_output: Optional[Dict[str, Any]] = None


def message_to_dict(message: BaseMessage) -> Dict[str, Any]:
    """Convert a message to dictionary format.

    This is a utility function for serialization.

    Args:
        message: Any message type

    Returns:
        Dictionary representation suitable for API calls or storage
    """
    return message.to_dict()


def dict_to_message(data: Dict[str, Any]) -> BaseMessage:
    """Create a message from a dictionary.

    Args:
        data: Dictionary with 'role' and 'content' keys

    Returns:
        Appropriate message type based on role
    """
    role = data.get("role", "user")
    content = data.get("content", "")

    if role == "system":
        return SystemMessage(content=content, name=data.get("name"))
    elif role == "user" or role == "human":
        return HumanMessage(content=content, name=data.get("name"))
    elif role == "assistant" or role == "ai":
        msg = AIMessage(content=content, name=data.get("name"))
        if "tool_calls" in data:
            msg.tool_calls = data["tool_calls"]
        return msg
    elif role == "tool":
        return ToolMessage(
            content=content,
            tool_call_id=data.get("tool_call_id", ""),
            name=data.get("name")
        )
    else:
        return BaseMessage(content=content, type=role)


def make_control_message(text: str, origin: str) -> "HumanMessage":
    """Create a user-role message that carries injected (non-user) control content.

    Records the true source via ``origin`` and, for non-user origins, wraps the
    text in an XML-ish envelope (e.g. ``<system-directive>``) so the model reads it
    as a distinct block rather than a genuine user turn. ``MessageOrigin.USER``
    returns the text unchanged (it IS a real user turn).

    Args:
        text: the message content.
        origin: a ``MessageOrigin`` value.

    Returns:
        A ``HumanMessage`` with ``origin`` set and content enveloped as needed.
    """
    tag = _ORIGIN_ENVELOPE.get(origin)
    content = f"<{tag}>\n{text}\n</{tag}>" if tag else text
    return HumanMessage(content=content, origin=origin)


# Convenience type aliases
Messages = List[BaseMessage]


__all__ = [
    "BaseMessage",
    "SystemMessage",
    "HumanMessage",
    "AIMessage",
    "ToolMessage",
    "MessageType",
    "MessageOrigin",
    "make_control_message",
    "ChatGeneration",
    "ChatResult",
    "message_to_dict",
    "dict_to_message",
    "Messages",
]
