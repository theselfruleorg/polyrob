"""``polyrob doctor`` names a configured model with no exact catalog row (106 K1)."""
import json
import sqlite3
from types import SimpleNamespace

from cli.commands.doctor_models import uncatalogued_model_lines


def _store(path, table, rows):
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE {table} (id TEXT, payload TEXT, enabled INT, status TEXT)")
    for row_id, payload, enabled, status in rows:
        con.execute(f"INSERT INTO {table} VALUES (?,?,?,?)",
                    (row_id, json.dumps(payload), enabled, status))
    con.commit()
    con.close()


def test_a_clean_config_prints_nothing(tmp_path):
    env = {"DEFAULT_MODEL": "deepseek/deepseek-v4-pro", "HEAVY_MODEL": "deepseek-v4-pro",
           "AUX_MODEL_JUDGE": "z-ai/glm-5.3-flash",
           "AUX_FALLBACK_JUDGE": "z-ai/glm-5.3-flash,openrouter/z-ai/glm-5.2"}
    assert uncatalogued_model_lines(env, str(tmp_path)) == []


def test_each_env_source_is_named(tmp_path):
    env = {"DEFAULT_MODEL": "no-row-a", "HEAVY_MODEL": "no-row-b",
           "AUX_MODEL_COMPACTION": "no-row-c", "AUX_FALLBACK_JUDGE": "openrouter/no-row-d"}
    text = "\n".join(uncatalogued_model_lines(env, str(tmp_path)))
    for model, source in (("no-row-a", "DEFAULT_MODEL"), ("no-row-b", "HEAVY_MODEL"),
                          ("no-row-c", "AUX_MODEL_COMPACTION"), ("no-row-d", "AUX_FALLBACK_JUDGE")):
        assert f"{model} has no exact row ({source})" in text


def test_a_family_fallback_is_not_a_row(tmp_path):
    lines = uncatalogued_model_lines({"DEFAULT_MODEL": "claude-sonnet-99"}, str(tmp_path))
    assert len(lines) == 1 and lines[0].startswith("! model catalog: claude-sonnet-99")


def test_job_and_goal_models_are_named_and_finished_rows_are_not(tmp_path):
    _store(tmp_path / "cron.db", "cron_jobs", [
        ("j1", {"model": "no-row-job"}, 1, "scheduled"),
        ("j2", {"model": "no-row-off"}, 0, "scheduled"),
        ("j3", {}, 1, "scheduled"),
    ])
    _store(tmp_path / "goals.db", "goals", [
        ("g1", {"model": "no-row-goal"}, 1, "ready"),
        ("g2", {"model": "no-row-done"}, 1, "done"),
    ])
    text = "\n".join(uncatalogued_model_lines({}, str(tmp_path)))
    assert "no-row-job has no exact row (job j1)" in text
    assert "no-row-goal has no exact row (goal g1)" in text
    assert "no-row-off" not in text and "no-row-done" not in text


def test_an_unreadable_store_is_named_not_read_as_empty(tmp_path):
    (tmp_path / "cron.db").write_text("this is not a database")
    lines = uncatalogued_model_lines({}, str(tmp_path))
    assert len(lines) == 1 and "could not read cron.db" in lines[0]


def test_the_short_doctor_view_prints_the_warning(tmp_path, monkeypatch, capsys):
    from cli.commands import doctor
    monkeypatch.setattr(doctor, "status_snapshot_lines", lambda: [])
    monkeypatch.setattr(doctor, "custody_environ", lambda: {"DEFAULT_MODEL": "no-row-a"})
    monkeypatch.setattr(doctor, "doctor_data_home", lambda env: str(tmp_path))
    doctor._emit_doctor(show_full=False, as_json=False)
    assert "no-row-a has no exact row (DEFAULT_MODEL)" in capsys.readouterr().out


def _pin_file(tmp_path, tenant, row):
    path = tmp_path / "identity" / "default" / tenant
    path.mkdir(parents=True)
    (path / "model.json").write_text(row if isinstance(row, str) else json.dumps(row))


def _session_file(tmp_path, session, status, model):
    path = tmp_path / "sessions" / "user-1" / session
    path.mkdir(parents=True)
    (path / "metadata.json").write_text(json.dumps({"status": status, "request": {"model": model}}))


def test_a_tenant_default_pin_is_named(tmp_path):
    _pin_file(tmp_path, "user_1", {"default": {"provider": "openrouter", "model": "acme/no-row-pin"}})
    text = "\n".join(uncatalogued_model_lines({}, str(tmp_path)))
    assert "acme/no-row-pin has no exact row (default of user_1)" in text


def test_an_unreadable_default_pin_is_named_not_read_as_empty(tmp_path):
    _pin_file(tmp_path, "user_1", "{not json")
    lines = uncatalogued_model_lines({}, str(tmp_path))
    assert len(lines) == 1 and "could not read the default model of user_1" in lines[0]


def test_a_live_session_pin_is_named_and_a_finished_one_is_not(tmp_path):
    _session_file(tmp_path, "s-live", "running", "acme/no-row-live")
    _session_file(tmp_path, "s-done", "completed", "acme/no-row-done")
    text = "\n".join(uncatalogued_model_lines({}, str(tmp_path)))
    assert "acme/no-row-live has no exact row (session s-live)" in text
    assert "no-row-done" not in text


def test_a_providers_yaml_model_is_named(tmp_path, monkeypatch):
    from modules.llm import provider_spec
    declared = SimpleNamespace(name="my-endpoint", models=("no-row-yaml",),
                               default_model="no-row-yaml", supports_vision=False)
    monkeypatch.setattr(provider_spec, "get_specs",
                        lambda: [*provider_spec.BUILTIN_SPECS, declared])
    text = "\n".join(uncatalogued_model_lines({}, str(tmp_path)))
    assert "no-row-yaml has no exact row (providers.yaml my-endpoint)" in text


def test_the_line_names_the_conservative_limits_and_the_zero_cost(tmp_path):
    lines = uncatalogued_model_lines({"DEFAULT_MODEL": "no-family-model"}, str(tmp_path))
    assert "128000 context and 8192 output tokens" in lines[0]
    assert "$0" in lines[0]


def test_the_line_names_the_family_limits(tmp_path):
    from modules.llm.model_registry import get_model_config
    row = get_model_config("claude-sonnet-99")
    lines = uncatalogued_model_lines({"DEFAULT_MODEL": "claude-sonnet-99"}, str(tmp_path))
    assert f"{row.context_window} context and {row.max_completion_tokens} output tokens" in lines[0]
    assert "family" in lines[0]
