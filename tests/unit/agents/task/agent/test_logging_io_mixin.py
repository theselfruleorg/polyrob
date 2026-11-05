"""P9 pass-3 — LoggingIOMixin extracted from service.py."""
from agents.task.agent.core.logging_io import LoggingIOMixin


def test_agent_composes_logging_io_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, LoggingIOMixin)
    for m in ("_log_response", "_log_context_breakdown", "_save_conversation",
              "_write_messages_to_file", "_write_response_to_file", "_log_agent_run",
              "_log_tool_outputs"):
        assert getattr(Agent, m).__qualname__.startswith("LoggingIOMixin")


class _Host(LoggingIOMixin):
    pass


def test_save_conversation_noop_without_path():
    h = _Host()
    h.save_conversation_path = None
    # must return without touching disk
    h._save_conversation([], object())


def test_log_tool_outputs_noop_without_path():
    h = _Host()
    h.tool_output_log_path = None
    h._log_tool_outputs([], [], 0)  # returns early, no raise


# --- the tool-output log is forensics, not the store of the result (102 D-1) ---

def _logging_host(path):
    import logging
    h = _Host()
    h.tool_output_log_path = path
    h.logger = logging.getLogger("test.logging_io.tool_outputs")
    return h


def _result(content):
    from types import SimpleNamespace
    return SimpleNamespace(extracted_content=content, error=None)


def _rows(path):
    import json
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_tool_output_log_caps_a_large_result_and_keeps_its_size(tmp_path):
    from agents.task.robust_parse_config import RobustParseConfig
    cap = RobustParseConfig.MAX_EXTRACTED_CONTENT_SIZE
    path = tmp_path / "tool_outputs.jsonl"
    _logging_host(path)._log_tool_outputs([{"name": "t"}], [_result("x" * (cap + 500))], 1)
    row = _rows(path)[0]["result"]
    assert len(row["content"]) == cap
    assert row["bytes"] == cap + 500
    assert row["truncated"] is True


def test_tool_output_log_leaves_a_small_result_whole(tmp_path):
    path = tmp_path / "tool_outputs.jsonl"
    _logging_host(path)._log_tool_outputs([{"name": "t"}], [_result("small")], 1)
    row = _rows(path)[0]["result"]
    assert row["content"] == "small" and row["bytes"] == 5
    assert "truncated" not in row


def test_tool_output_log_does_not_copy_a_large_list_into_first_item(tmp_path):
    """One huge list element would otherwise carry the whole result past the cap."""
    import json
    from agents.task.robust_parse_config import RobustParseConfig
    cap = RobustParseConfig.MAX_EXTRACTED_CONTENT_SIZE
    path = tmp_path / "tool_outputs.jsonl"
    content = json.dumps(["y" * (cap + 500), "z"])
    _logging_host(path)._log_tool_outputs([{"name": "t"}], [_result(content)], 1)
    row = _rows(path)[0]["result"]
    assert row["item_count"] == 2
    assert "first_item" not in row
    assert path.stat().st_size < cap + 2000


def test_tool_output_log_stops_writing_at_its_size_limit(tmp_path, monkeypatch, caplog):
    import logging
    from agents.task.agent.core import logging_io
    monkeypatch.setattr(logging_io, "TOOL_OUTPUT_LOG_MAX_BYTES", 100)
    path = tmp_path / "tool_outputs.jsonl"
    h = _logging_host(path)
    with caplog.at_level(logging.WARNING, logger=h.logger.name):
        for step in range(5):
            h._log_tool_outputs([{"name": "t"}], [_result("a" * 200)], step)
    assert len(_rows(path)) == 1, "the row that reaches the limit is the last one"
    stops = [r for r in caplog.records if "stopped" in r.getMessage()]
    assert len(stops) == 1, "the stop is named once, not once per step"


def test_tool_output_log_still_reports_the_count_after_the_stop(tmp_path, monkeypatch, caplog):
    import json
    import logging
    from agents.task.agent.core import logging_io
    monkeypatch.setattr(logging_io, "TOOL_OUTPUT_LOG_MAX_BYTES", 1)
    path = tmp_path / "tool_outputs.jsonl"
    path.write_text("full\n")
    h = _logging_host(path)
    with caplog.at_level(logging.INFO, logger=h.logger.name):
        h._log_tool_outputs([{"name": "t"}], [_result(json.dumps([1, 2, 3]))], 1)
    assert path.read_text() == "full\n"
    assert any("3 items" in r.getMessage() for r in caplog.records)


def test_a_payload_over_the_parse_limit_is_not_parsed(tmp_path, monkeypatch):
    import json
    import types
    from agents.task.agent.core import logging_io
    monkeypatch.setattr(logging_io, "JSON_PARSE_MAX_CHARS", 10)
    path = tmp_path / "tool_outputs.jsonl"
    result = types.SimpleNamespace(extracted_content=json.dumps(list(range(50))), error=None)
    _logging_host(str(path))._log_tool_outputs([{"name": "t"}], [result], 1)
    row = json.loads(path.read_text().splitlines()[0])["result"]
    assert "item_count" not in row
    assert row["bytes"] == len(result.extracted_content)


def test_a_payload_within_the_parse_limit_is_counted(tmp_path):
    import json
    import types
    path = tmp_path / "tool_outputs.jsonl"
    result = types.SimpleNamespace(extracted_content=json.dumps([1, 2, 3]), error=None)
    _logging_host(str(path))._log_tool_outputs([{"name": "t"}], [result], 1)
    assert json.loads(path.read_text().splitlines()[0])["result"]["item_count"] == 3
