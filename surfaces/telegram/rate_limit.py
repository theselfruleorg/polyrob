"""Telegram-specific rate limiting - MINIMAL approach.

Best practice from Telegram/grammY docs:
- DON'T pre-throttle requests artificially
- DO respect RetryAfter errors when they occur
- Make requests as fast as possible, handle 429s gracefully

This module only tracks penalties from actual Telegram RetryAfter responses.
It does NOT try to predict or prevent rate limits.
"""

import asyncio
import time
import logging
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ChatPenalty:
    """Track active penalty for a chat."""
    chat_id: int
    penalty_until: float = 0.0
    penalty_reason: str = ""


class TelegramRateLimiter:
    """Minimal rate limiter - only tracks actual Telegram penalties.

    Philosophy (from grammY docs):
    "It is harmful to artificially delay some requests in order to avoid
    hitting limits because the performance of your bot would be far from optimal."

    We ONLY:
    1. Track when Telegram tells us to wait (RetryAfter)
    2. Block requests during active penalties
    3. Let everything else through immediately
    """

    def __init__(self):
        """Initialize rate limiter."""
        self._penalties: Dict[int, ChatPenalty] = {}
        self._lock = asyncio.Lock()
        logger.info("TelegramRateLimiter initialized (minimal mode - respects RetryAfter only)")

    async def check_edit_allowed(
        self,
        chat_id: int,
        message_id: Optional[int] = None
    ) -> Tuple[bool, Optional[float]]:
        """Check if an edit is allowed (only blocked during active penalty).

        Returns:
            Tuple of (allowed: bool, wait_seconds: Optional[float])
        """
        async with self._lock:
            current_time = time.time()

            # Check if under active penalty
            if chat_id in self._penalties:
                penalty = self._penalties[chat_id]
                if current_time < penalty.penalty_until:
                    wait_time = penalty.penalty_until - current_time
                    return False, wait_time
                else:
                    # Penalty expired, clean up
                    del self._penalties[chat_id]

            # No penalty - allow immediately
            return True, None

    async def record_edit(self, chat_id: int, message_id: Optional[int] = None):
        """Record a successful edit (no-op in minimal mode)."""
        pass  # We don't track successful edits, only penalties

    async def record_penalty(
        self,
        chat_id: int,
        retry_after: float,
        operation: str = "edit"
    ):
        """Record a rate limit penalty from Telegram RetryAfter.

        Args:
            chat_id: Telegram chat ID
            retry_after: Seconds to wait (from TelegramRetryAfter exception)
            operation: Type of operation that was rate limited
        """
        async with self._lock:
            current_time = time.time()

            self._penalties[chat_id] = ChatPenalty(
                chat_id=chat_id,
                penalty_until=current_time + retry_after,
                penalty_reason=f"{operation} rate limited for {retry_after}s"
            )

            logger.warning(
                f"Telegram penalty recorded for chat {chat_id}: "
                f"wait {retry_after:.1f}s"
            )

    async def check_send_allowed(self) -> Tuple[bool, Optional[float]]:
        """Check if send is allowed (always yes in minimal mode)."""
        return True, None

    async def record_send(self):
        """Record a send (no-op in minimal mode)."""
        pass

    async def is_chat_penalized(self, chat_id: int) -> Tuple[bool, float]:
        """Check if a chat is currently under penalty."""
        async with self._lock:
            if chat_id not in self._penalties:
                return False, 0.0

            penalty = self._penalties[chat_id]
            current_time = time.time()

            if current_time < penalty.penalty_until:
                return True, penalty.penalty_until - current_time

            # Expired
            del self._penalties[chat_id]
            return False, 0.0

    async def get_chat_stats(self, chat_id: int) -> Dict:
        """Get stats for a chat."""
        penalized, remaining = await self.is_chat_penalized(chat_id)
        return {
            "chat_id": chat_id,
            "penalized": penalized,
            "penalty_remaining": remaining
        }

    async def get_global_stats(self) -> Dict:
        """Get global stats."""
        async with self._lock:
            current_time = time.time()
            active_penalties = sum(
                1 for p in self._penalties.values()
                if current_time < p.penalty_until
            )
            return {
                "active_penalties": active_penalties,
                "tracked_chats": len(self._penalties)
            }

    async def reset_chat(self, chat_id: int):
        """Clear penalty for a chat."""
        async with self._lock:
            if chat_id in self._penalties:
                del self._penalties[chat_id]


# Global singleton
_rate_limiter: Optional[TelegramRateLimiter] = None


def get_telegram_rate_limiter() -> TelegramRateLimiter:
    """Get the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = TelegramRateLimiter()
    return _rate_limiter
