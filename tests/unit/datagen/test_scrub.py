"""Wave 1 Task 2 — fail-closed scrub gate."""
import pytest

from datagen.record import TrajectoryRecord
from datagen.scrub import (
    ScrubError,
    has_correspondent_content,
    scrub_record,
    strip_images,
)


def _rec(**kw):
    defaults = dict(session_id="s1", messages=[], steps=[])
    defaults.update(kw)
    return TrajectoryRecord(**defaults)


def test_scrub_redacts_secret_shapes():
    rec = _rec(messages=[{"type": "ToolMessage",
                          "content": "OPENAI_API_KEY=sk-aaaaaaaaaaaaaaaaaaaaaaaa"}])
    out = scrub_record(rec)
    assert "sk-aaaaaaaaaaaaaaaaaaaaaaaa" not in out.messages[0]["content"]
    assert "<secret>redacted</secret>" in out.messages[0]["content"]


def test_scrub_walks_tool_calls_and_steps_and_task():
    rec = _rec(
        task="use Bearer abcdefgh12345678 please",
        messages=[{"type": "AIMessage", "content": "",
                   "tool_calls": [{"function": {
                       "arguments": '{"password": "hunter2secret"}'}}]}],
        steps=[{"result": [{"extracted_content":
                            "token=verysecretvalue123"}]}],
    )
    out = scrub_record(rec)
    assert "abcdefgh12345678" not in out.task
    args = out.messages[0]["tool_calls"][0]["function"]["arguments"]
    assert "hunter2secret" not in args
    assert "verysecretvalue123" not in out.steps[0]["result"][0]["extracted_content"]


def test_scrub_error_is_fail_closed(monkeypatch):
    import datagen.scrub as scrub_mod

    def boom(_):
        raise RuntimeError("scrubber exploded")

    monkeypatch.setattr(scrub_mod, "scrub_secret_shapes", boom)
    with pytest.raises(ScrubError):
        scrub_record(_rec(messages=[{"content": "x"}]))


def test_strip_images_multimodal_list():
    content = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "hi"},
    ]
    out = strip_images(content)
    assert out[0] == {"type": "text", "text": "[image]"}
    assert out[1] == {"type": "text", "text": "hi"}


def test_strip_images_data_uri_string():
    assert strip_images("data:image/png;base64,AAAA") == "[image]"
    assert strip_images("plain text") == "plain text"


def test_has_correspondent_content():
    assert has_correspondent_content(
        _rec(messages=[{"content": "x", "origin": "CORRESPONDENT"}]))
    assert has_correspondent_content(
        _rec(messages=[{"content": "x",
                        "origin": "MessageOrigin.CORRESPONDENT"}]))
    assert not has_correspondent_content(
        _rec(messages=[{"content": "x", "origin": "USER"}]))


# --- finalization coverage: shapes in innocent fields; nested arguments ------


def test_shaped_secret_in_non_credential_field_is_caught():
    """A provider-shaped key is redacted even under an innocent field name —
    the shape scrubber, not the key name, is what catches it."""
    rec = _rec(messages=[{"type": "AIMessage",
                          "content": "note to self: sk-abcdefabcdefabcdef12 works"}])
    out = scrub_record(rec)
    assert "sk-abcdefabcdefabcdef12" not in out.messages[0]["content"]


def test_secret_deep_inside_nested_tool_arguments_dict():
    rec = _rec(messages=[{
        "type": "AIMessage", "content": "",
        "tool_calls": [{"id": "c1", "function": {
            "name": "http_request",
            "arguments": {"headers": {"authorization":
                                      "Bearer abcdefgh12345678"}}}}],
    }])
    out = scrub_record(rec)
    args = out.messages[0]["tool_calls"][0]["function"]["arguments"]
    assert "abcdefgh12345678" not in str(args)


def test_shapeless_secret_in_innocent_field_passes_documented_limitation():
    """Documented conservative contract (core/secret_patterns.py): a raw
    string with NO recognizable shape under a non-credential key survives.
    Pinned so a future generic-entropy scrubber changes this test on purpose."""
    rec = _rec(messages=[{"type": "AIMessage",
                          "content": "the passphrase is hunter2secret"}])
    out = scrub_record(rec)
    assert "hunter2secret" in out.messages[0]["content"]
