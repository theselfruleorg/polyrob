"""Wave 1 Task 5 — polyrob datagen CLI wrapper."""
import click
import pytest
from click.testing import CliRunner

from cli.commands.datagen import _parse_filters, datagen


def test_parse_filters():
    assert _parse_filters(("outcome=done", "verified=verified")) == {
        "outcome": "done", "verified": "verified"}


def test_parse_filters_rejects_bare_key():
    with pytest.raises(click.BadParameter):
        _parse_filters(("outcome",))


def test_datagen_export_command(tmp_path, monkeypatch):
    import json

    sdir = tmp_path / "u1" / "sessions" / "s1" / "memory"
    sdir.mkdir(parents=True)
    (sdir / "message_history.json").write_text(json.dumps({
        "session_id": "s1", "saved_at": "t",
        "messages": [{"type": "HumanMessage", "content": "x"}]}))

    class _FakePM:
        data_root = tmp_path

    monkeypatch.setattr("agents.task.path.pm", lambda: _FakePM())
    out = tmp_path / "corpus.jsonl"
    result = CliRunner().invoke(
        datagen, ["export", "-o", str(out), "--format", "raw"])
    assert result.exit_code == 0, result.output
    assert "Exported 1 session(s)" in result.output
    assert out.exists()
