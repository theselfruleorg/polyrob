"""026 P1.4 — enum-shaped flags reject invalid members with the valid set.

Failure mode G: `config set AUTONOMY_MODE autonmous` wrote cleanly and
`policy.autonomy_mode()` degraded the typo to `supervised` — no error at
either end. All three writers (config_service, `polyrob config set`, REPL
`/config set`) and `config check` now consult FLAG_ENUMS.
"""
import pytest

from core.config_policy.flag_enums import FLAG_ENUMS, enum_error
from core.config_policy.policy import _AUTONOMY_MODES, _AUTONOMY_POSTURES


def test_tables_track_the_policy_resolvers():
    assert FLAG_ENUMS["AUTONOMY_MODE"] == tuple(_AUTONOMY_MODES)
    assert FLAG_ENUMS["AUTONOMY_POSTURE"] == tuple(_AUTONOMY_POSTURES)


def test_enum_error_shapes():
    assert enum_error("AUTONOMY_MODE", "autonomous") is None
    assert enum_error("AUTONOMY_MODE", "AUTONOMOUS") is None  # case-insensitive
    err = enum_error("AUTONOMY_MODE", "autonmous")
    assert err is not None and "supervised, autonomous" in err
    assert enum_error("NOT_AN_ENUM_FLAG", "whatever") is None
    assert enum_error("MEMORY_BACKEND", "") is None  # blank = unset idiom
    assert enum_error("AGENT_COMPUTE_POSTURE", "9") is not None


def test_every_enum_key_is_a_documented_flag():
    from core.prefs import catalog_lookup
    for key in FLAG_ENUMS:
        assert catalog_lookup(key) is not None, f"{key} missing from the catalog"


def test_config_service_rejects_enum_typo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from core.config_service import set_value
    result = set_value("AUTONOMY_MODE", "autonmous", scope="project")
    assert result.ok is False and result.outcome == "invalid"
    assert "supervised, autonomous" in result.message
    ok = set_value("AUTONOMY_MODE", "supervised", scope="project")
    assert ok.ok is True


def test_cli_config_set_rejects_enum_typo(tmp_path, monkeypatch):
    import click.testing
    from cli.commands.config import config
    monkeypatch.chdir(tmp_path)
    runner = click.testing.CliRunner()
    result = runner.invoke(config, ["set", "AUTONOMY_POSTURE", "fulll"])
    assert result.exit_code != 0
    assert "silent, owner-visible, full" in result.output


def test_repl_config_set_rejects_enum_typo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from cli.ui.commands.h_config import ConfigCtx, cmd_config
    out = cmd_config(ConfigCtx(user_id="local", home_dir=str(tmp_path)),
                     ["set", "PAYMENT_APPROVAL_MODE", "yolo"])
    assert "error:" in out and "approve, auto" in out


def test_config_check_flags_enum_typo(tmp_path):
    env = tmp_path / ".env"
    env.write_text("AUTONOMY_MODE=autonmous\n")
    from core.prefs import check_env_files
    findings = check_env_files([env])
    assert any("supervised, autonomous" in f for f in findings)
