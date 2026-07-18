"""Wave 1 Task 4 — session export reads memory/ history + training formats."""
import json

from cli.commands.session import _assemble_export_data, _render_training_format


def _mk_session(tmp_path, memory_layout=True):
    sdir = tmp_path / "u1" / "sessions" / "sess-9"
    payload = {
        "session_id": "sess-9",
        "saved_at": "2026-07-11T09:00:00",
        "messages": [
            {"type": "HumanMessage", "content": "hello", "origin": "USER"},
            {"type": "AIMessage", "content": "hi there"},
        ],
    }
    target = (sdir / "memory" / "message_history.json") if memory_layout \
        else (sdir / "message_history.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))
    return sdir


def test_assemble_export_reads_memory_subdir(tmp_path):
    sdir = _mk_session(tmp_path, memory_layout=True)
    data = _assemble_export_data("sess-9", None, sdir, "2026-07-11T10:00:00")
    assert len(data["messages"]["messages"]) == 2


def test_assemble_export_legacy_root_still_works(tmp_path):
    sdir = _mk_session(tmp_path, memory_layout=False)
    data = _assemble_export_data("sess-9", None, sdir, "2026-07-11T10:00:00")
    assert len(data["messages"]["messages"]) == 2


def test_render_training_format_sharegpt(tmp_path):
    sdir = _mk_session(tmp_path)
    out = _render_training_format("sess-9", {"model": "m1"}, sdir, "sharegpt")
    assert [c["from"] for c in out["conversations"]] == ["human", "gpt"]
    assert out["metadata"]["session_id"] == "sess-9"


def test_render_training_format_scrubs(tmp_path):
    sdir = _mk_session(tmp_path)
    hist = sdir / "memory" / "message_history.json"
    payload = json.loads(hist.read_text())
    payload["messages"][1]["content"] = "key OPENAI_API_KEY=sk-aaaaaaaaaaaaaaaaaaaaaaaa"
    hist.write_text(json.dumps(payload))
    out = _render_training_format("sess-9", None, sdir, "openai")
    assert "sk-aaaaaaaaaaaaaaaaaaaaaaaa" not in json.dumps(out)
