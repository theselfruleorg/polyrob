import inspect
from core.surfaces.dispatcher import RouteDecision, RouteKind
from surfaces.telegram import harness


def test_route_is_turn():
    assert harness._route_is_turn(RouteDecision(RouteKind.STEER, "k"))
    assert harness._route_is_turn(RouteDecision(RouteKind.TASK_AGENT, "k"))
    assert not harness._route_is_turn(RouteDecision(RouteKind.DENIED, "k", silent=True))
    assert not harness._route_is_turn(RouteDecision(RouteKind.COMMAND, "k", command="/x"))


def test_visible_side_effects_are_gated_on_a_turn():
    # W9: the echo and the WORKING bubble live in the pre-turn phase.
    src = inspect.getsource(harness.TelegramHarness._pre_turn_feedback)
    echo = src.index("voice_transcript_echo_enabled()")
    working = src.index("ProgressStage.WORKING")
    assert src.rfind("_route_is_turn(result.decision)", 0, echo) != -1
    assert src.rfind("_route_is_turn(result.decision)", 0, working) != -1


def test_anonymous_group_sender_is_denied():
    src = inspect.getsource(harness.TelegramHarness._gate_raw_update)  # W9 phase 1
    assert "if tg_id is None and _tg_chat_type(update) != \"private\"" in src
