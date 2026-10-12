"""044 T2: owner-only output never lands in a room."""
import inspect

from surfaces.telegram import harness


def test_command_reply_target_is_owner_only_aware():
    src = inspect.getsource(harness.TelegramHarness._reply_targets)  # W9 phase 4
    assert "owner_only_reply_target(" in src


def test_bootstrap_reply_never_targets_a_group():
    src = inspect.getsource(harness.TelegramHarness._gate_raw_update)  # W9 phase 1
    # the bootstrap branch must check the chat type before replying
    assert "_tg_chat_type(update) == \"private\"" in src
