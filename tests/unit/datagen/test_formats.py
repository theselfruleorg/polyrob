"""Wave 1 Task 3 — training-format renderers (sharegpt / openai / raw)."""
import json

from datagen.formats import FORMATS, render_openai, render_raw, render_sharegpt
from datagen.record import TrajectoryRecord


def _record():
    return TrajectoryRecord(
        session_id="s1",
        model="m1",
        labels={"outcome": "done", "verified": "verified"},
        usage={"records": 2, "total_tokens": 100},
        provenance={"source": "export"},
        messages=[
            {"type": "SystemMessage", "content": "you are rob"},
            {"type": "HumanMessage", "content": "send hi", "origin": "USER"},
            {"type": "AIMessage", "content": "sending now",
             "tool_calls": [{"id": "c1", "function": {
                 "name": "send_message",
                 "arguments": '{"text": "hi"}'}}]},
            {"type": "ToolMessage", "content": "sent", "tool_call_id": "c1"},
        ],
    )


def test_formats_registry():
    assert set(FORMATS) == {"raw", "sharegpt", "openai"}


def test_render_raw_roundtrips_record():
    out = render_raw(_record())
    assert out["schema_version"] == 1
    assert out["session_id"] == "s1"
    assert len(out["messages"]) == 4


def test_render_sharegpt_roles_and_tool_blocks():
    out = render_sharegpt(_record())
    convs = out["conversations"]
    assert [c["from"] for c in convs] == ["system", "human", "gpt", "tool"]
    gpt = convs[2]["value"]
    assert gpt.startswith("<think>")
    assert "<tool_call>" in gpt and "</tool_call>" in gpt
    call = json.loads(gpt.split("<tool_call>")[1].split("</tool_call>")[0])
    assert call["name"] == "send_message"
    assert call["arguments"] == {"text": "hi"}
    tool = convs[3]["value"]
    assert "<tool_response>" in tool
    resp = json.loads(tool.split("<tool_response>")[1].split("</tool_response>")[0])
    assert resp["tool_call_id"] == "c1"
    assert resp["name"] == "send_message"
    assert resp["content"] == "sent"
    assert out["labels"]["outcome"] == "done"
    assert out["metadata"]["session_id"] == "s1"


def test_render_sharegpt_control_origin_is_human():
    rec = _record()
    rec.messages.append({"type": "HumanMessage", "content": "<skill-catalog/>",
                         "origin": "SKILL"})
    out = render_sharegpt(rec)
    assert out["conversations"][-1]["from"] == "human"


def test_render_openai_structured_tool_calls():
    out = render_openai(_record())
    msgs = out["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "send_message"
    assert msgs[3]["tool_call_id"] == "c1"
    assert out["labels"]["verified"] == "verified"


def test_multimodal_content_flattened():
    rec = _record()
    rec.messages[1]["content"] = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,A"}},
        {"type": "text", "text": "look"},
    ]
    sg = render_sharegpt(rec)
    assert sg["conversations"][1]["value"] == "[image]\nlook"
    oa = render_openai(rec)
    assert oa["messages"][1]["content"] == "[image]\nlook"


# --- finalization coverage: multimodal+tool_calls, unpaired tool result ------


def test_sharegpt_multimodal_ai_message_with_tool_calls():
    rec = TrajectoryRecord(
        session_id="s2",
        messages=[
            {"type": "AIMessage",
             "content": [{"type": "text", "text": "looking"},
                         {"type": "image_url",
                          "image_url": {"url": "data:image/png;base64,AAAA"}}],
             "tool_calls": [{"id": "c9", "function": {
                 "name": "screenshot", "arguments": "{}"}}]},
        ],
    )
    out = render_sharegpt(rec)
    value = out["conversations"][0]["value"]
    assert "looking" in value
    assert "<tool_call>" in value and '"name": "screenshot"' in value
    assert "base64,AAAA" not in value  # images stripped, never exported


def test_openai_multimodal_ai_message_keeps_structured_tool_calls():
    rec = TrajectoryRecord(
        session_id="s2",
        messages=[
            {"type": "AIMessage",
             "content": [{"type": "text", "text": "looking"}],
             "tool_calls": [{"id": "c9", "function": {
                 "name": "screenshot", "arguments": "{}"}}]},
        ],
    )
    out = render_openai(rec)
    msg = out["messages"][0]
    assert msg["content"] == "looking"
    assert msg["tool_calls"][0]["function"]["name"] == "screenshot"


def test_tool_result_with_unseen_call_id_gets_empty_name():
    rec = TrajectoryRecord(
        session_id="s3",
        messages=[
            {"type": "ToolMessage", "content": "orphan result",
             "tool_call_id": "never-registered"},
        ],
    )
    out = render_sharegpt(rec)
    payload = json.loads(
        out["conversations"][0]["value"]
        .removeprefix("<tool_response>\n").removesuffix("\n</tool_response>"))
    assert payload["tool_call_id"] == "never-registered"
    assert payload["name"] == ""  # resolves empty, never crashes
    assert payload["content"] == "orphan result"
