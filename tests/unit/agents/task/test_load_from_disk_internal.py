"""Regression: load_from_disk must restore AI/Tool messages via the internal path.

Bug (2026-07-01): `MessagePersistenceMixin.load_from_disk` re-added persisted messages
with `self.add_message(message)` (public path). The single-writer guard rejects
AIMessage/ToolMessage there, so restoring a real session's history raised MessageFlowError
and aborted the whole load → a warm STEER resume (e.g. an owner Telegram voice message)
got stuck 'initializing' and never replied. It must pass `_internal=True`.
"""
import pathlib

_PERSISTENCE = (
    pathlib.Path(__file__).resolve().parents[4]
    / "agents" / "task" / "agent" / "messages" / "persistence.py"
)


def test_load_from_disk_adds_messages_internally():
    text = _PERSISTENCE.read_text()
    # the restore loop must bypass the single-writer guard
    assert "self.add_message(message, _internal=True)" in text
    # and must NOT use the guarded public form there
    assert "self.add_message(message)\n" not in text
