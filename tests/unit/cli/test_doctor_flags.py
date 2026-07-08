"""`polyrob doctor --flags` — resolved env-flag dump (Wave D / SA-05)."""
import click.testing
import pytest

from cli.commands.doctor import doctor, flags_report


@pytest.fixture(autouse=True)
def _clean_posture_env(monkeypatch):
    # dynamic defaults read the process env via agents.task.constants helpers
    for var in ("AUTONOMY_POSTURE", "POLYROB_LOCAL", "ROB_LOCAL", "GOAL_COMPLETION_JUDGE"):
        monkeypatch.delenv(var, raising=False)


def test_flags_report_groups_and_lines():
    lines = flags_report({})
    text = "\n".join(lines)
    assert "LLM / providers" in text
    assert any("GEMINI_PROMPT_CACHE" in ln for ln in lines)
    # every flag line shows a source
    flag_lines = [ln for ln in lines if "=" in ln]
    assert flag_lines and all("[" in ln and "]" in ln for ln in flag_lines)


def test_flags_report_masks_secrets():
    lines = flags_report({"ANYSITE_API_KEY": "sk-verysecret"})
    joined = "\n".join(lines)
    assert "verysecret" not in joined
    assert "(set, masked)" in joined


def test_flags_report_reflects_posture(monkeypatch):
    monkeypatch.setenv("AUTONOMY_POSTURE", "owner-visible")
    lines = flags_report({"AUTONOMY_POSTURE": "owner-visible"})
    judge = next(ln for ln in lines if "GOAL_COMPLETION_JUDGE" in ln)
    assert "True" in judge
    assert "posture:owner-visible" in judge


def test_flags_report_env_wins(monkeypatch):
    monkeypatch.setenv("AUTONOMY_POSTURE", "owner-visible")
    lines = flags_report(
        {"AUTONOMY_POSTURE": "owner-visible", "GOAL_COMPLETION_JUDGE": "false"}
    )
    judge = next(ln for ln in lines if "GOAL_COMPLETION_JUDGE" in ln)
    assert "False" in judge and "[env]" in judge


def test_doctor_flags_cli_smoke():
    runner = click.testing.CliRunner()
    result = runner.invoke(doctor, ["--flags"])
    assert result.exit_code == 0
    assert "GOALS_ENABLED" in result.output
    assert "resolved flags" in result.output


def test_flags_report_cli_context_matches_doctor_report(monkeypatch):
    """doctor and doctor --flags must tell ONE story: with POLYROB_LOCAL absent
    everywhere, doctor_report says local is ON (CLI setdefault semantics) — the
    flags report must resolve the local-derived defaults the same way."""
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    lines = flags_report({}, local_absent_means_on=True)
    goals = next(ln for ln in lines if "GOALS_ENABLED" in ln)
    assert "True" in goals and "local=ON" in goals
    # and the server context resolves them OFF
    lines = flags_report({}, local_absent_means_on=False)
    goals = next(ln for ln in lines if "GOALS_ENABLED" in ln)
    assert "False" in goals


def test_flags_report_local_derived_extras(monkeypatch):
    # local-mode-derived flags OUTSIDE _SAFE_LOCAL_FLAGS get the dynamic default too
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    lines = flags_report({"POLYROB_LOCAL": "1"})
    ticker = next(ln for ln in lines if "TICKER_IDLE_BACKOFF_ENABLED" in ln)
    assert "True" in ticker and "local=ON" in ticker
