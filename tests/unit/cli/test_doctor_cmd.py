from click.testing import CliRunner

from cli.commands.doctor import doctor, doctor_report


def test_doctor_report_lists_present_keys_and_flags():
    env = {"ANTHROPIC_API_KEY": "sk-x", "POLYROB_LOCAL": "1"}
    lines = doctor_report(env)
    blob = "\n".join(lines)
    assert "anthropic: present" in blob
    assert "openai: missing" in blob
    assert "POLYROB_LOCAL" in blob and "ON" in blob          # footgun surfaced
    assert "resolved provider" in blob.lower()


def test_doctor_report_warns_no_keys():
    lines = doctor_report({})
    blob = "\n".join(lines)
    assert "no provider API key" in blob  # case-sensitive; brief had blob.lower() typo


def test_doctor_command_runs():
    res = CliRunner().invoke(doctor, [], env={"ANTHROPIC_API_KEY": "sk-x"})
    assert res.exit_code == 0, res.output
    assert "anthropic: present" in res.output


def test_doctor_flags_present_but_malformed_key():
    # A present-but-too-short key must be annotated as malformed AND must not be the
    # resolved provider (runtime rejects it) — doctor's core job is diagnosing this,
    # not giving false confidence.
    lines = doctor_report({"ANTHROPIC_API_KEY": "x"})
    blob = "\n".join(lines)
    assert "malformed" in blob.lower()
    resolved = [l for l in lines if l.startswith("resolved provider/model:")][0]
    assert "anthropic" not in resolved


def test_doctor_polyrob_local_defaults_on_when_absent():
    # The CLI (build_cli_container) defaults POLYROB_LOCAL=1, so an ABSENT value means
    # ON for run/chat — doctor must report that (the footgun it exists to surface),
    # not 'off'.
    lines = doctor_report({})
    assert any("POLYROB_LOCAL: ON" in l for l in lines)


def test_doctor_polyrob_local_explicit_off_reads_off():
    lines = doctor_report({"POLYROB_LOCAL": "0"})
    assert any("POLYROB_LOCAL: off" in l for l in lines)


def test_doctor_report_shows_resolved_env():
    lines = doctor_report({"CONFIG_ENV": "production"})
    assert any("resolved env: production" in ln for ln in lines)


def test_doctor_command_sees_production_only_key(tmp_path, monkeypatch):
    # Criterion #3: `polyrob doctor` loads env like the REPL, so a key that lives only
    # in config/.env.production is reported present (via the local-mode backfill).
    for k in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY",
        "CONFIG_ENV", "ENV",
    ):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / ".env.production").write_text("OPENROUTER_API_KEY=sk-or-doc\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(doctor, [])
    assert res.exit_code == 0, res.output
    assert "openrouter: present" in res.output
