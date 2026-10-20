"""present_choice waits up to ``wait_seconds`` for the owner's tap, but the
controller's generic per-action timeout (180s) cancelled it first: prod
2026-10-15 04:09-04:12Z, the owner answered a 300s choice and the turn sat
until "present_choice timed out after 180 seconds". The controller must give
the action at least its own declared wait.
"""
from tools.controller.card_actions import DEFAULT_WAIT_S, choice_action_timeout


def test_declared_wait_outlasts_the_generic_timeout():
    assert choice_action_timeout({"wait_seconds": 300}, 180) > 300


def test_default_wait_when_unspecified():
    assert choice_action_timeout({}, 180) > DEFAULT_WAIT_S


def test_never_shrinks_a_longer_base():
    assert choice_action_timeout({"wait_seconds": 10}, 600) == 600


def test_bad_value_falls_back_to_default():
    assert choice_action_timeout({"wait_seconds": "x"}, 180) > DEFAULT_WAIT_S
    assert choice_action_timeout(None, 180) > DEFAULT_WAIT_S
