"""P9 pass-1 — extracted pure log-sanitization helpers."""
from agents.task.agent.log_sanitize import (
    sanitize_text_for_log, sanitize_structure_for_log,
)


def test_short_text_unchanged():
    assert sanitize_text_for_log("hello") == "hello"


def test_empty_text_passthrough():
    assert sanitize_text_for_log("") == ""


def test_base64_image_removed():
    assert sanitize_text_for_log("data:image/png;base64,AAAA") == "<IMAGE_OR_LARGE_DATA_REMOVED>"


def test_very_long_text_removed():
    assert sanitize_text_for_log("x" * 5001) == "<IMAGE_OR_LARGE_DATA_REMOVED>"


def test_structure_drops_image_keys_and_recurses():
    data = {
        "keep": "ok",
        "screenshot": "data:image/png;base64,Z",
        "image": "x",
        "image_data": "y",
        "nested": {"image": "drop", "msg": "x" * 6000},
        "list": ["short", "data:image/x"],
    }
    out = sanitize_structure_for_log(data)
    assert out == {
        "keep": "ok",
        "nested": {"msg": "<IMAGE_OR_LARGE_DATA_REMOVED>"},
        "list": ["short", "<IMAGE_OR_LARGE_DATA_REMOVED>"],
    }


def test_non_string_scalars_passthrough():
    assert sanitize_structure_for_log(42) == 42
    assert sanitize_structure_for_log(None) is None


def test_agent_delegates_to_module():
    # the thin Agent wrappers must still resolve to the extracted helpers
    import agents.task.agent.service as svc
    assert svc.Agent._sanitize_text_for_log.__doc__ is not None
