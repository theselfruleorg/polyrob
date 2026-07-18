"""Wave 2 Task 3 — polyrob datagen run CLI wrapper."""
import json

from click.testing import CliRunner

import cli.commands.datagen as dg


def test_datagen_run_rejects_unknown_distribution(tmp_path):
    tasks = tmp_path / "t.jsonl"
    tasks.write_text(json.dumps({"prompt": "x"}))
    result = CliRunner().invoke(
        dg.datagen, ["run", "--tasks", str(tasks), "--distribution", "bogus"])
    assert result.exit_code == 2
    assert "Unknown distribution" in result.output


def _unset_tracked(monkeypatch, key):
    """Leave *key* unset for the test AND registered for teardown restore.

    ``monkeypatch.delenv(key, raising=False)`` on an ABSENT key records no
    undo, so a value written later by the code under test (the command's
    ``os.environ.setdefault``) would leak process-wide into later tests
    (e.g. SELF_WAKE_ENABLED=false broke test_commands_autonomy). setenv
    first registers the undo-to-absent; delenv then clears it for the test.
    """
    monkeypatch.setenv(key, "tracked")
    monkeypatch.delenv(key)


def test_datagen_run_sets_hygiene_env_and_runs(tmp_path, monkeypatch):
    tasks = tmp_path / "t.jsonl"
    tasks.write_text(json.dumps({"prompt": "x"}))
    for key in dg._DATAGEN_HYGIENE_ENV:
        _unset_tracked(monkeypatch, key)

    seen = {}

    async def fake_run(tasks_path, run_name, *args):
        import os
        seen["memory_backend"] = os.environ.get("MEMORY_BACKEND")
        seen["capture"] = os.environ.get("TRAJECTORY_CAPTURE")
        seen["run_name"] = run_name
        return {"total": 1, "completed": 1, "failed": 0, "spend_usd": 0.0}

    monkeypatch.setattr(dg, "_datagen_run_async", fake_run)
    result = CliRunner().invoke(
        dg.datagen, ["run", "--tasks", str(tasks), "--name", "myrun"])
    assert result.exit_code == 0, result.output
    assert seen["memory_backend"] == "none"
    assert seen["capture"] == "false"
    assert seen["run_name"] == "myrun"
    assert "1/1 completed" in result.output


def test_explicit_env_wins_over_hygiene_default(tmp_path, monkeypatch):
    tasks = tmp_path / "t.jsonl"
    tasks.write_text(json.dumps({"prompt": "x"}))
    for key in dg._DATAGEN_HYGIENE_ENV:
        _unset_tracked(monkeypatch, key)
    monkeypatch.setenv("MEMORY_BACKEND", "sqlite")

    async def fake_run(*args):
        import os
        return {"total": 0, "completed": 0, "failed": 0,
                "spend_usd": 0.0, "mb": os.environ["MEMORY_BACKEND"]}

    monkeypatch.setattr(dg, "_datagen_run_async", fake_run)
    result = CliRunner().invoke(dg.datagen, ["run", "--tasks", str(tasks)])
    assert result.exit_code == 0
    import os
    assert os.environ["MEMORY_BACKEND"] == "sqlite"  # setdefault semantics
