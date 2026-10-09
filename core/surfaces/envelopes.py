"""Typed value objects for the Surface contract. Transport-free.

Session keys are CHAT-scoped: a group chat is one shared session,
a DM is isolated by user. Identity rides on the message, never in the key.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MessageKind(str, Enum):
    AGENT_TEXT = "agent_text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ASK = "ask"
    STREAM_DELTA = "stream_delta"
    SYSTEM_NOTE = "system_note"


@dataclass
class SessionSource:
    surface_id: str
    chat_id: str
    chat_type: str = "dm"          # "dm" | "group" | "channel"
    thread_id: Optional[str] = None


@dataclass
class Identity:
    user_id: str                   # internal user_id (NEVER the raw platform id)
    source: SessionSource
    raw_user_id: Optional[str] = None   # platform id (e.g. tg_id), audit only
    display_name: Optional[str] = None
    # 044 T16: the per-chat role resolved ONCE at the routing boundary
    # (core/surfaces/access.py::resolve_access_tier) —
    # "owner" | "admin" | "member" | "blocked". None = not resolved yet (a DM,
    # or an inbound that never reached the tier model): every reader falls back
    # to owner-or-member rather than inventing a privilege.
    chat_role: Optional[str] = None
    # CHAT-6: on a FORGEABLE surface (email) the surface says whether the
    # sender address is proven (the receiving MX's Authentication-Results).
    # Anything but True keeps such a sender at DENIED in resolve_access_tier.
    # Ignored on every non-forgeable surface.
    sender_authenticated: Optional[bool] = None


@dataclass
class InboundMessage:
    text: str
    identity: Identity
    idempotency_key: Optional[str] = None   # e.g. Telegram update_id
    kind: str = "comment"
    media: list = field(default_factory=list)
    reply_to: Optional[str] = None
    raw: Optional[dict] = None              # escape hatch
    internal: bool = False                  # synthetic (self-wake/delegation)
    mentions_bot: Optional[bool] = None     # W3 groups: True/False when the surface
                                            # can detect mentions; None = unknown
                                            # (treated as NOT mentioned by the gate)
    sender_is_bot: bool = False             # 044 T8: the sender is itself a bot
                                            # (Telegram from.is_bot); default False
                                            # preserves every existing surface/test
    forwarded: bool = False                 # H06 (2026-09-23): the surface saw a
                                            # FORWARDED message. Its text is quoted
                                            # third-party DATA (already wrapped by the
                                            # surface), never a COMMAND and never a
                                            # pending decision or stop phrase.


#: ``Action.style`` values a renderer may map to a button colour.
ACTION_STYLES = ("default", "primary", "danger")


@dataclass(frozen=True)
class Action:
    """One tappable choice on an outbound message (064 F2).

    ``command`` is EXACTLY what the owner could type: a one-token tappable
    command (``/approve_p_a1b2c3``) or a ``core/verbs.py`` verb (``/status``).
    Anything else is refused at construction — a button is a new input path,
    and it may only ever say what a typed message could. A press is routed as
    the presser typing ``command``; the presser comes from the platform's
    authenticated identity, never from the button.
    """
    label: str
    command: str
    style: str = "default"

    def __post_init__(self):
        from core.surfaces.actions import is_action_command
        if not is_action_command(self.command):
            raise ValueError(f"not a tappable command or verb: {self.command!r}")
        if self.style not in ACTION_STYLES:
            raise ValueError(f"unknown action style: {self.style!r}")
        if not (self.label or "").strip():
            raise ValueError("an action needs a label")


@dataclass
class OutboundMessage:
    session_key: str
    text: str
    kind: MessageKind = MessageKind.AGENT_TEXT
    partial: bool = False                   # True = stream delta; False = committed
    stream_id: Optional[str] = None
    reply_to: Optional[str] = None
    # Outbound media contract (Task 7, G-40): a renderable entry is
    #   {"kind": "image" | "document", "path": "<local file path>", "caption": str | None}
    # `path` must resolve inside the CURRENT session's workspace — producers (e.g. the
    # `message()` tool) validate this before it ever reaches a surface. A surface skips
    # any entry that lacks a `path` (not renderable) and any path that doesn't exist /
    # isn't readable, logging a WARN — media delivery is fail-open, text is not.
    # The legacy email-subject entry `{"subject": ...}` remains legal (EmailSurface.send
    # reads media[0]["subject"]) but is NOT a renderable media entry.
    media: list = field(default_factory=list)
    #: 064 F2: tappable choices (``Action``). A surface with
    #: ``supports_actions`` renders them as buttons; the TEXT keeps the same
    #: commands, so a surface without actions renders byte-equal to before.
    actions: list = field(default_factory=list)


#: ``ReplyWindow.kind`` values (064 F4). How long, after the user last spoke,
#: the platform lets the bot answer freely:
#: ``service_window`` (WhatsApp 24 h, template outside), ``token`` (a one-shot
#: reply token — LINE), ``session_webhook`` (a per-conversation URL with a TTL —
#: DingTalk), ``stream`` (an open stream — QQ passive reply), ``context_token``
#: (a per-peer token that must ride every reply — WeChat iLink).
REPLY_WINDOW_KINDS = ("token", "session_webhook", "stream", "context_token", "service_window")


@dataclass(frozen=True)
class ReplyWindow:
    kind: str                     # one of REPLY_WINDOW_KINDS
    ttl_s: int                    # seconds after the last inbound
    outside: str = "template_only"   # SendDecision value outside the window


@dataclass
class SurfaceCapabilities:
    supports_streaming: bool = False
    supports_edit: bool = False
    supports_interactive_ask: bool = False
    is_multi_tenant: bool = False
    max_message_bytes: int = 4096
    markdown_flavor: str = "none"           # "none" | "markdown_v2" | "html"
    service_window_secs: int = 0            # >0 = business-initiated send window (WhatsApp 24h)
    requires_template_outside_window: bool = False  # outside the window, only templates send
    media_out: bool = False                 # can render OutboundMessage.media (photo/attachment)
    reply_window: Optional[ReplyWindow] = None   # 064 F4: None = free outbound
    supports_actions: bool = False          # 064 F2: renders OutboundMessage.actions as buttons
    voice_out: Optional[str] = None         # 064 F5: voice-note format ("ogg_opus"); None = no voice

    def effective_reply_window(self) -> Optional[ReplyWindow]:
        """``reply_window``, else the legacy ``service_window_secs`` pair."""
        if self.reply_window is not None:
            return self.reply_window
        if self.service_window_secs > 0:
            return ReplyWindow("service_window", self.service_window_secs,
                               "template_only" if self.requires_template_outside_window
                               else "deny")
        return None


@dataclass
class SendResult:
    success: bool
    surface_message_id: Optional[str] = None
    error: Optional[str] = None
    #: OB7 — on a PARTIAL failure, the text that did NOT go. ``None`` = the
    #: surface does not know its progress (the whole message is retried);
    #: ``""`` = every word landed and only a best-effort extra failed (the
    #: message counts as delivered); otherwise the retry sends only this text.
    remaining_text: Optional[str] = None
    #: 0008 — EVERY id the send produced (each text chunk, each media item), in
    #: order; ``surface_message_id`` stays the last one. What the post ledger
    #: records so the agent can later delete the whole post.
    surface_message_ids: list = field(default_factory=list)
