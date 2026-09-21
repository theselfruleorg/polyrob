import os

import pytest
from click.testing import CliRunner

from cli.commands.doctor import doctor, doctor_report


def _group_exists(name: str) -> bool:
    try:
        import grp
        grp.getgrnam(name)
        return True
    except (KeyError, ImportError):
        return False


_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

def test_doctor_report_lists_present_keys_and_flags():
    env = {"ANTHROPIC_API_KEY": "sk-x", "POLYROB_LOCAL": "1"}
    lines = doctor_report(env)
    blob = "\n".join(lines)
    assert "anthropic: present" in blob
    # 30+ providers ship now, so an UNCONFIGURED one is rolled into a single
    # "not configured:" line instead of getting a line of its own — thirty
    # "missing" lines buried the one or two that actually mattered.
    assert "not configured:" in blob
    assert "openai" in blob
    assert "POLYROB_LOCAL" in blob and "ON" in blob          # footgun surfaced
    assert "resolved provider" in blob.lower()


def test_doctor_report_warns_no_keys():
    lines = doctor_report({})
    blob = "\n".join(lines)
    # 024 L1.5 widened "key" to "credential" (a connected account is neither an
    # env var nor an API key), but the phrase people actually search for has to
    # survive that rename — say both.
    assert "no provider credential found" in blob
    assert "no API key" in blob
    assert "polyrob init" in blob


def test_doctor_command_runs():
    # 043 A13: plain `doctor` leads with the status snapshot and hides the
    # check transcript (incl. "anthropic: present") behind --full.
    res = CliRunner().invoke(doctor, ["--full"], env={"ANTHROPIC_API_KEY": "sk-x"})
    assert res.exit_code == 0, res.output
    assert "anthropic: present" in res.output


def test_doctor_command_plain_leads_with_snapshot_and_hides_transcript():
    res = CliRunner().invoke(doctor, [], env={"ANTHROPIC_API_KEY": "sk-x"})
    assert res.exit_code == 0, res.output
    assert "anthropic: present" not in res.output
    assert "run `polyrob doctor --full` for every check" in res.output


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
    # The suite-wide operator-env sandbox (tests/conftest.py) disables the
    # backfill; this test exercises exactly that mechanism (against ITS OWN
    # fake config/.env.production above), so opt back in explicitly.
    monkeypatch.setenv("POLYROB_ENV_KEY_BACKFILL", "1")
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(doctor, ["--full"])
    assert res.exit_code == 0, res.output
    assert "openrouter: present" in res.output


def test_doctor_malformed_env_key_names_the_unset_remedy():
    # "present but unusable — malformed" is a diagnosis with no verb: the fix
    # (remove the bad value) must be named right on the line, now that
    # `polyrob config unset` exists.
    lines = doctor_report({"ANTHROPIC_API_KEY": "x"})
    blob = "\n".join(lines)
    assert "polyrob config unset ANTHROPIC_API_KEY" in blob


# --- WS-G (057 R6): `doctor --perms` -------------------------------------

def _perms_home(tmp_path, monkeypatch, mode=0o2770):
    import os
    h = tmp_path / "datahome"
    h.mkdir()
    (h / "auto").mkdir()
    os.chmod(h, mode)
    os.chmod(h / "auto", mode)
    monkeypatch.setattr("cli._admin_home.admin_data_dir", lambda **kw: str(h))
    return h


def _own_group():
    import grp
    import os
    return grp.getgrgid(os.getgid()).gr_name


@pytest.mark.skipif(_group_exists("polyrob-data"),
                    reason="this box HAS the polyrob-data group (a deployed box); the skip path needs its absence")
def test_doctor_perms_skips_when_the_group_is_absent(tmp_path, monkeypatch):
    """A dev checkout has no `polyrob-data` group: SKIP, never a false pass
    and never a failure."""
    _perms_home(tmp_path, monkeypatch)
    res = CliRunner().invoke(doctor, ["--perms"])
    assert res.exit_code == 0, res.output
    assert "not checked" in res.output


def test_doctor_perms_strict_exits_nonzero_on_an_offender(tmp_path, monkeypatch):
    import os
    h = _perms_home(tmp_path, monkeypatch)
    bad = h / "auto" / "root_written.db"
    bad.write_text("x")
    os.chmod(bad, 0o644)
    import core.data_perms as dp
    real = dp.audit_data_perms
    monkeypatch.setattr(dp, "audit_data_perms",
                        lambda d, group=_own_group(), **kw: real(d, group=group, **kw))
    res = CliRunner().invoke(doctor, ["--perms", "--strict"])
    assert res.exit_code == 1, res.output
    assert "root_written.db" in res.output
    assert "remedy:" in res.output


@pytest.mark.skipif(_IS_ROOT, reason="run as root every tmp entry is root-owned, which IS the offender class")
def test_doctor_perms_strict_is_zero_when_clean(tmp_path, monkeypatch):
    _perms_home(tmp_path, monkeypatch)
    import core.data_perms as dp
    real = dp.audit_data_perms
    monkeypatch.setattr(dp, "audit_data_perms",
                        lambda d, group=_own_group(), **kw: real(d, group=group, **kw))
    res = CliRunner().invoke(doctor, ["--perms", "--strict"])
    assert res.exit_code == 0, res.output
    assert "data perms: OK" in res.output


def test_doctor_perms_json_is_machine_readable(tmp_path, monkeypatch):
    import json
    _perms_home(tmp_path, monkeypatch)
    res = CliRunner().invoke(doctor, ["--perms", "--json"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.output)
    assert set(doc) >= {"data_dir", "group", "skipped", "ok", "offender_total",
                        "counts", "offenders", "remedy", "report"}
