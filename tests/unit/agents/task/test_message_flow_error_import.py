"""Regression: message_manager must not import a non-existent `BaseError`.

Bug (2026-07-01): `agents/task/agent/message_manager/service.py` did
`from core.exceptions import BaseError` (which doesn't exist — the base is `BotError`).
This raised ImportError inside `add_message`, crashing `load_from_disk` when a session
resumed — so an owner STEER (e.g. a Telegram voice message resuming a warm session)
got no response. Guard the exact symbol.
"""
import pathlib

import core.exceptions as exc

_SERVICE = (
    pathlib.Path(__file__).resolve().parents[4]
    / "agents" / "task" / "agent" / "message_manager" / "service.py"
)


def test_core_exceptions_has_boterror_not_baseerror():
    assert hasattr(exc, "BotError")
    assert not hasattr(exc, "BaseError")


def test_message_manager_imports_boterror_not_baseerror():
    text = _SERVICE.read_text()
    assert "import BaseError" not in text
    assert "MessageFlowError(BaseError)" not in text
    # the single-writer guard subclasses the real base exception
    assert "from core.exceptions import BotError" in text
    assert "class MessageFlowError(BotError)" in text
