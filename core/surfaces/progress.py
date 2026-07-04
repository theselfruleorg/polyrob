"""Surface-agnostic in-turn progress reporter.

A ProgressReporter shows a transient status bubble while an agent turn runs
('🎤 Transcribing…' → '⚙️ Working…') and removes it when the answer arrives. It is
NOT a MessageRouter producer: editing/deleting a sent status needs the
surface_message_id, which MessageRouter.publish() does not return. So the surface that
owns the transport hands the reporter three async callables (send/edit/delete) and the
reporter drives them directly. Every transport op is fail-open: a status bubble must
NEVER break or block a real turn.
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class ProgressStage:
    """Preset stage strings (extensible)."""
    TRANSCRIBING = "🎤 Transcribing your voice message…"
    WORKING = "⚙️ Working…"


class ProgressReporter(ABC):
    @abstractmethod
    async def stage(self, text: str) -> None: ...

    @abstractmethod
    async def finish(self) -> None: ...


class NullProgressReporter(ProgressReporter):
    """No-op reporter for surfaces that don't (yet) implement progress, or when there
    is no chat to post to."""

    async def stage(self, text: str) -> None:
        return None

    async def finish(self) -> None:
        return None


# Transport callable shapes (all async):
#   send(text)             -> surface_message_id | None
#   edit(message_id, text) -> None
#   delete(message_id)     -> None
SendFn = Callable[[str], Awaitable[Optional[Any]]]
EditFn = Callable[[Any, str], Awaitable[None]]
DeleteFn = Callable[[Any], Awaitable[None]]


class EditingProgressReporter(ProgressReporter):
    """First stage() SENDS a status message and captures its id; later stage()s EDIT it
    in place (or send a fresh note when supports_edit=False); finish() DELETES it. One
    status bubble per reporter instance.

    Invariants:
      - finish() is idempotent (the _finished flag) so it can be called from several
        control-flow branches without double-deleting or racing.
      - stage() after finish() is a no-op (a late WORKING can't resurrect a deleted
        bubble).
      - stage() with the same text as the last is skipped (Telegram rejects an edit to
        identical text with a 400 'message is not modified').
      - a failed initial send leaves _message_id=None, so the next stage() retries a
        send rather than editing a phantom id.
    All transport ops are fail-open.
    """

    def __init__(self, send: SendFn, edit: EditFn, delete: DeleteFn,
                 *, supports_edit: bool = True) -> None:
        self._send = send
        self._edit = edit
        self._delete = delete
        self._supports_edit = supports_edit
        self._message_id: Optional[Any] = None
        self._last_text: Optional[str] = None
        self._finished = False

    async def stage(self, text: str) -> None:
        if self._finished or text == self._last_text:
            return
        try:
            if self._message_id is None:
                self._message_id = await self._send(text)
                self._last_text = text
            elif self._supports_edit:
                await self._edit(self._message_id, text)
                self._last_text = text
            else:
                # No edit support: send a fresh note, track the newest id so finish()
                # removes the most recent status bubble.
                self._message_id = await self._send(text)
                self._last_text = text
        except Exception as e:  # fail-open
            logger.debug("progress.stage failed: %s", e)

    async def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        mid = self._message_id
        self._message_id = None
        if mid is None:
            return
        try:
            await self._delete(mid)
        except Exception as e:  # fail-open
            logger.debug("progress.finish (delete) failed: %s", e)
