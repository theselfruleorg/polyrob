"""044 T2: owner-only output never lands in a room."""
import inspect

from surfaces.telegram import harness


def test_command_reply_target_is_owner_only_aware():
    src = inspect.getsource(harness.TelegramHarness.handle_update)
    assert "owner_only_reply_target(" in src


def test_bootstrap_reply_never_targets_a_group():
    src = inspect.getsource(harness.TelegramHarness.handle_update)
    # the bootstrap branch must check the chat type before replying
    assert "_tg_chat_type(update) == \"private\"" in src
