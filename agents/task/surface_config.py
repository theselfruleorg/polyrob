"""Feature flags for the Singular Chat Interface (Surface contract).

Read through this class (NOT BotConfig.get, which is getattr-backed and returns
the default for any unknown key -> a gate read that way is permanently off).
Reuses constants._bool_env so default-ON and default-OFF flags share one parser.
"""
import os
from typing import Optional

from agents.task.constants import _bool_env, local_mode_enabled


def _int_env(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(v) if v is not None and v.strip() != "" else default
    except (TypeError, ValueError):
        return default


class SurfaceConfig:
    @staticmethod
    def singular_chat_enabled() -> bool:
        return _bool_env("SINGULAR_CHAT_ENABLED", False)

    @staticmethod
    def telegram_surface_enabled() -> bool:
        return _bool_env("TELEGRAM_SURFACE_ENABLED", False)

    @staticmethod
    def whatsapp_surface_enabled() -> bool:
        return _bool_env("WHATSAPP_SURFACE_ENABLED", False)

    @staticmethod
    def email_surface_enabled() -> bool:
        """Run the email surface (IMAP poll inbound + SMTP outbound). Default OFF.
        v1 is correspondent-only — owner-by-email stays OFF until verified-sender lands."""
        return _bool_env("EMAIL_SURFACE_ENABLED", False)

    @staticmethod
    def email_imap_poll_sec() -> int:
        """Seconds between IMAP polls for new mail (no IDLE in v1)."""
        return _int_env("EMAIL_IMAP_POLL_SEC", 60)

    @staticmethod
    def chat_intent_classifier_enabled() -> bool:
        return _bool_env("CHAT_INTENT_CLASSIFIER", False)

    # --- WS-A three-tier access model (owner/correspondent/denied) -----------
    @staticmethod
    def correspondent_access_enabled() -> bool:
        """Resolve every inbound to an access tier (OWNER/CORRESPONDENT/DENIED) at the
        routing boundary. Default OFF — when off, route_inbound is byte-identical to
        today (only the legacy pairing gate applies). When on, a non-owner is routable
        only as a known correspondent (its reply is DATA delivered to the originating
        session, never a steering turn)."""
        return _bool_env("CORRESPONDENT_ACCESS_ENABLED", False)

    @staticmethod
    def correspondent_require_approval() -> bool:
        """A newly auto-seeded correspondent is PENDING (owner must approve) before its
        replies are routable. Default ON — a third party the agent contacted does not
        become a trusted-data channel until the owner ratifies it. Set
        CORRESPONDENT_REQUIRE_APPROVAL=false for single-user/local convenience."""
        return _bool_env("CORRESPONDENT_REQUIRE_APPROVAL", True)

    @staticmethod
    def correspondent_max_new_per_day() -> int:
        """Per-tenant cap on NEW correspondents seeded per 24h — bounds the blast radius
        of an injection that tricks the agent into mass-contacting addresses."""
        return _int_env("CORRESPONDENT_MAX_NEW_PER_DAY", 20)

    # --- #9 voice transcription ---------------------------------------------
    @staticmethod
    def voice_transcription_enabled() -> bool:
        """Transcribe inbound voice/audio messages to text before routing (#9).
        Default ON: when the faster-whisper extra is installed, voice notes are
        transcribed; when it isn't, the surface degrades gracefully (the inbound guard
        tells the user voice is unavailable instead of routing an empty turn).
        Set VOICE_TRANSCRIPTION_ENABLED=false to turn it off."""
        return _bool_env("VOICE_TRANSCRIPTION_ENABLED", True)

    @staticmethod
    def voice_transcription_model() -> str:
        """faster-whisper model size: tiny|base|small|medium|large-v3. `base` balances
        CPU latency vs accuracy for short voice notes."""
        return (os.getenv("VOICE_TRANSCRIPTION_MODEL") or "base").strip()

    @staticmethod
    def voice_transcription_required() -> bool:
        """When ON, a missing faster-whisper engine is a startup ERROR (not just WARN).
        Default OFF: most deploys degrade gracefully (voice refused with a notice)."""
        return _bool_env("VOICE_TRANSCRIPTION_REQUIRED", False)

    @staticmethod
    def voice_transcript_echo_enabled() -> bool:
        """Echo the transcript back into the chat as a persistent, voice-note-anchored
        message before the agent answers (Telegram + WhatsApp). Default ON; purely additive.
        Set VOICE_TRANSCRIPT_ECHO=false for byte-identical prior behavior."""
        return _bool_env("VOICE_TRANSCRIPT_ECHO", True)

    # --- #8 Telegram incremental streaming ----------------------------------
    @staticmethod
    def telegram_incremental_stream() -> bool:
        """Live `editMessageText` streaming on Telegram (#8). Default OFF — the buffered
        one-send-on-finalize path stays the safe default; opt in per deployment."""
        return _bool_env("TELEGRAM_INCREMENTAL_STREAM", False)

    @staticmethod
    def telegram_stream_edit_interval_sec() -> float:
        """Minimum seconds between live stream edits (flood-control). Telegram tolerates
        only a few edits/sec; 1.5s is conservative. `0` edits on every delta (tests)."""
        try:
            v = os.getenv("TELEGRAM_STREAM_EDIT_INTERVAL_SEC")
            return float(v) if v is not None and v.strip() != "" else 1.5
        except (TypeError, ValueError):
            return 1.5

    # --- P0.1 session-boundary policy ---------------------------------------
    @staticmethod
    def session_reset_mode() -> str:
        """idle | daily | both | none. Default `idle` everywhere (#7): a fresh thread
        after a cooldown is natural and memory recall bridges the gap. The server flip
        (none->idle) is gated on the recreate-race (#2) + mute-on-resume (#0) fixes,
        both landed, so a reset can't drop a reply or double-build an orchestrator.
        Pin `SESSION_RESET_MODE=none` to restore the legacy inert behavior; `daily` is
        opt-in (server-local-tz skew). The idle WINDOW still differs by profile
        (`session_idle_minutes`: 720 local / 1440 server)."""
        return (os.getenv("SESSION_RESET_MODE") or "idle").strip().lower()

    @staticmethod
    def session_idle_minutes() -> int:
        """Idle threshold; a chat older than this since last activity starts fresh.
        Default 1440 (24h); 720 (12h) under POLYROB_LOCAL (a personal bot rolls sooner)."""
        return _int_env("SESSION_IDLE_MINUTES", 720 if local_mode_enabled() else 1440)

    @staticmethod
    def session_reset_hour() -> int:
        """Local hour (0-23) for the daily session roll."""
        return _int_env("SESSION_RESET_HOUR", 4)

    # --- outbound durability -------------------------------------------------
    @staticmethod
    def outbound_queue_enabled() -> bool:
        """Route final outbound messages through the durable OutboundDeliveryQueue
        (retry + dead-letter). Default OFF -> publish() sends directly (legacy). Streaming
        deltas are always best-effort (never queued)."""
        return _bool_env("OUTBOUND_QUEUE_ENABLED", False)

    # --- a5 surface GC -------------------------------------------------------
    @staticmethod
    def surface_gc_enabled() -> bool:
        """Periodic GC of stale chat<->session bindings (a5). Default ON under the local
        profile when the chat bus is on; OFF on the multi-tenant server until validated."""
        return _bool_env(
            "SURFACE_GC_ENABLED",
            local_mode_enabled() and SurfaceConfig.singular_chat_enabled(),
        )

    @staticmethod
    def surface_gc_horizon_secs() -> int:
        """Bindings with no activity for longer than this are purged. ``max(2x idle
        window, 7d)`` so GC never races the idle-reset boundary — a pointer is only
        dropped well after it would already start a fresh thread."""
        return max(SurfaceConfig.session_idle_minutes() * 60 * 2, 7 * 86400)

    # --- webhook signing secrets ------------------------------------------------
    @staticmethod
    def whatsapp_template_name() -> str:
        """Approved utility template used to re-open the 24h window for a proactive msg."""
        return (os.getenv("WHATSAPP_TEMPLATE_NAME") or "task_ready").strip()

    @staticmethod
    def webhook_secret(surface_id: str) -> "Optional[str]":
        """Read {SURFACE}_WEBHOOK_SECRET from env. Surface id is upper-cased:
        whatsapp -> WHATSAPP_WEBHOOK_SECRET. Returns stripped value or None."""
        v = os.getenv(f"{surface_id.upper()}_WEBHOOK_SECRET")
        return v.strip() if v and v.strip() else None

    @staticmethod
    def webhook_verify_token(surface_id: str) -> "Optional[str]":
        """Read {SURFACE}_VERIFY_TOKEN from env. Surface id is upper-cased:
        whatsapp -> WHATSAPP_VERIFY_TOKEN. Returns stripped value or None."""
        v = os.getenv(f"{surface_id.upper()}_VERIFY_TOKEN")
        return v.strip() if v and v.strip() else None
