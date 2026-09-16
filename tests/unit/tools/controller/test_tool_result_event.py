"""043 A16/A28 — the typed ``tool_result`` feed event.

``_capture_tool_telemetry`` reads ``ActionResult.metadata`` (not only
``extracted_content``), derives a ``{kind, payload}`` render, and emits a
``tool_result`` feed event that carries the render + the artifact id alongside
the legacy ``result_preview``/``result_size``/``result_truncated``/``call_id``.
The pre-existing ``tool_execution`` telemetry stays byte-identical.
"""
import logging
from types import SimpleNamespace

import core.artifacts as artmod
from tools.controller.execution import ExecutionMixin
from tools.controller.types import ActionResult


def _mixin_and_capture(monkeypatch, ledger=None, current_step=3):
    """An ExecutionMixin wired to record the feed writes and telemetry calls."""
    captured = {"feed": [], "exec": []}

    def _add_to_feed(session_id, event_type, data):
        captured["feed"].append((session_id, event_type, data))

    def _capture_tool_execution(**kwargs):
        captured["exec"].append(kwargs)

    telemetry_manager = SimpleNamespace(capture_tool_execution=_capture_tool_execution)
    session_manager = SimpleNamespace(add_to_feed=_add_to_feed)
    orch = SimpleNamespace(
        telemetry_manager=telemetry_manager,
        session_manager=session_manager,
        current_step=current_step,
    )
    mixin = ExecutionMixin.__new__(ExecutionMixin)
    mixin.orchestrator = orch
    mixin.session_id = "s1"
    mixin.logger = logging.getLogger("test_tool_result_event")

    if ledger is not None:
        monkeypatch.setattr(artmod, "get_artifact_ledger", lambda: ledger)
    return mixin, captured


def _feed_data(captured, event_type="tool_result"):
    for _sid, etype, data in captured["feed"]:
        if etype == event_type:
            return data
    raise AssertionError(f"no {event_type} feed event was emitted: {captured['feed']}")


def test_tool_result_carries_render_and_artifact_id(monkeypatch, tmp_path):
    # An unresolvable artifact id is still carried on the event; the render
    # falls back to a text kind (the id can't be classified, but the preview is).
    ledger = artmod.ArtifactLedger(str(tmp_path / "artifacts.db"))
    mixin, captured = _mixin_and_capture(monkeypatch, ledger=ledger)

    result = ActionResult(extracted_content="18 passed", metadata={"artifact_id": "a_x"})
    ctx = SimpleNamespace(user_id="u1")
    mixin._capture_tool_telemetry(
        "run_tests", "coding", {}, 0.2, True, result, execution_context=ctx,
    )

    data = _feed_data(captured)
    assert data["render"]["kind"] == "text"
    assert data["artifact_id"] == "a_x"
    # The legacy preview string still rides on the same event.
    assert data["result_preview"] == "18 passed"
    assert data["call_id"] is None


def test_csv_artifact_renders_as_table(monkeypatch, tmp_path):
    ledger = artmod.ArtifactLedger(str(tmp_path / "artifacts.db"))
    csv = tmp_path / "prices.csv"
    csv.write_text("a,b\n1,2\n")
    art = ledger.record("u1", str(csv), session_id="s1", kind=artmod.kind_for_path(str(csv)))
    assert art is not None and art.kind == artmod.KIND_DATA

    mixin, captured = _mixin_and_capture(monkeypatch, ledger=ledger)
    result = ActionResult(extracted_content="wrote prices.csv", metadata={"artifact_id": art.id})
    ctx = SimpleNamespace(user_id="u1")
    mixin._capture_tool_telemetry(
        "write_file", "filesystem", {}, 0.1, True, result, execution_context=ctx,
    )

    data = _feed_data(captured)
    assert data["render"]["kind"] == "table"
    assert data["render"]["payload"]["path"] == "prices.csv"
    assert data["artifact_id"] == art.id


def test_no_metadata_keeps_preview_byte_identical(monkeypatch, tmp_path):
    ledger = artmod.ArtifactLedger(str(tmp_path / "artifacts.db"))
    mixin, captured = _mixin_and_capture(monkeypatch, ledger=ledger)

    result = ActionResult(extracted_content="hello world")
    ctx = SimpleNamespace(user_id="u1")
    mixin._capture_tool_telemetry(
        "some_action", "sometool", {}, 0.3, True, result, execution_context=ctx,
    )

    # tool_execution telemetry is emitted with the raw preview (unchanged).
    assert captured["exec"], "tool_execution telemetry was not captured"
    assert captured["exec"][0]["result_preview"] == "hello world"
    # tool_result carries the same preview and a plain text render, no artifact.
    data = _feed_data(captured)
    assert data["render"] == {"kind": "text", "payload": {"text": "hello world"}}
    assert data["artifact_id"] is None
    assert data["result_preview"] == "hello world"


def test_error_result_renders_as_error(monkeypatch, tmp_path):
    ledger = artmod.ArtifactLedger(str(tmp_path / "artifacts.db"))
    mixin, captured = _mixin_and_capture(monkeypatch, ledger=ledger)

    result = ActionResult(extracted_content="boom")
    ctx = SimpleNamespace(user_id="u1")
    mixin._capture_tool_telemetry(
        "read_file", "filesystem", {}, 0.1, False, result,
        execution_context=ctx, error="file not found",
    )

    data = _feed_data(captured)
    assert data["render"]["kind"] == "error"
    assert data["render"]["payload"]["error"] == "file not found"
    assert data["success"] is False
