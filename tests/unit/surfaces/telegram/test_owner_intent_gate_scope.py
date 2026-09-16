"""044 T1: the deterministic owner stop gate runs only on a routed turn.

A DENIED (silent group) decision must never apply a pause nor reply into the
room — the review found "stop everything" typed in ANY group, unlisted and
unmentioned, applied a real pause and posted the confirmation publicly."""
from core.surfaces.dispatcher import RouteKind
from surfaces.telegram import harness


def test_intent_gate_kinds_exclude_denied_and_command():
    kinds = harness._INTENT_GATE_KINDS
    assert RouteKind.STEER in kinds
    assert RouteKind.TASK_AGENT in kinds
    assert RouteKind.DENIED not in kinds
    assert RouteKind.COMMAND not in kinds
    assert RouteKind.CORRESPONDENT_DATA not in kinds


def test_guard_source_uses_the_kind_set():
    import inspect
    src = inspect.getsource(harness.TelegramHarness.handle_update)
    assert "_INTENT_GATE_KINDS" in src
    assert "!= RouteKind.COMMAND" not in src
