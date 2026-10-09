"""026 P2 — one write path: `polyrob config set`, REPL `/config set` and
`core.config_service.set_value` agree, because the first two call the third.

One table of (key, value) -> expected outcome, run against all three entrypoints
into three fresh project dirs; the resulting env files must be identical.
"""
import inspect
from pathlib import Path

import pytest
from click.testing import CliRunner

CASES = [
    # key, value, expect_ok
    ("CODE_EXEC_ENABLED", "true", True),                 # bool flag
    ("TWITTER_POST_COOLDOWN_SEC", "900", True),          # numeric flag
    ("TWITTER_POST_COOLDOWN_SEC", "soon", False),        # shape mismatch
    ("AUTONOMY_MODE", "autonmous", False),               # enum typo
    ("AUTONOMY_MODE", "supervised", True),               # enum member
    ("NOT_A_REAL_FLAG_W4", "1", False),                  # unknown key
    ("CODE_EXEC_ENABLE", "true", False),                 # near-miss typo
]


def _proj(tmp_path, monkeypatch, name):
    d = tmp_path / name
    d.mkdir()
    monkeypatch.chdir(d)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / f"{name}-home"))
    return d


def _env(d: Path) -> str:
    from core.paths import polyrob_home
    p = polyrob_home() / ".env"
    return p.read_text() if p.exists() else ""


def _via_cli(key, value):
    from cli.commands.config import config
    r = CliRunner().invoke(config, ["set", key, value])
    return r.exit_code == 0, r.output


def _via_repl(tmp_path, key, value):
    from cli.ui.commands.h_config import ConfigCtx, cmd_config
    out = cmd_config(ConfigCtx(user_id="u1", home_dir=tmp_path), ["set", key, value])
    ok = out.startswith("Set ")
    return ok, out


def _via_service(key, value):
    from core.config_service import set_value
    res = set_value(key, value, scope="global", surface="local")
    return res.ok, res.message


@pytest.mark.parametrize("key,value,expect_ok", CASES)
def test_three_entrypoints_agree(tmp_path, monkeypatch, key, value, expect_ok):
    monkeypatch.delenv(key, raising=False)
    outcomes, files = [], []
    d = _proj(tmp_path, monkeypatch, "cli")
    outcomes.append(_via_cli(key, value)[0])
    files.append(_env(d))
    d = _proj(tmp_path, monkeypatch, "repl")
    outcomes.append(_via_repl(tmp_path, key, value)[0])
    files.append(_env(d))
    d = _proj(tmp_path, monkeypatch, "svc")
    outcomes.append(_via_service(key, value)[0])
    files.append(_env(d))
    assert outcomes == [expect_ok] * 3, outcomes
    assert files[0] == files[1] == files[2], files
    if expect_ok:
        assert f"{key}={value}" in files[0]


def test_unknown_key_refusals_share_the_suggestion(tmp_path, monkeypatch):
    _proj(tmp_path, monkeypatch, "x")
    _, cli_out = _via_cli("CODE_EXEC_ENABLE", "true")
    _, repl_out = _via_repl(tmp_path, "CODE_EXEC_ENABLE", "true")
    _, svc_out = _via_service("CODE_EXEC_ENABLE", "true")
    for out in (cli_out, repl_out, svc_out):
        assert "CODE_EXEC_ENABLED" in out, out


def test_force_is_the_service_allow_unknown(tmp_path, monkeypatch):
    d = _proj(tmp_path, monkeypatch, "f")
    from cli.commands.config import config
    r = CliRunner().invoke(config, ["set", "NOT_A_REAL_FLAG_W4", "1", "--force"])
    assert r.exit_code == 0, r.output
    assert "NOT_A_REAL_FLAG_W4=1" in _env(d)
    from core.config_service import set_value
    assert not set_value("NOT_A_REAL_FLAG_W4", "1", surface="console",
                         allow_unknown=True).ok   # local-only escape hatch


def test_repl_global_scope(tmp_path, monkeypatch):
    _proj(tmp_path, monkeypatch, "g")
    monkeypatch.delenv("CODE_EXEC_ENABLED", raising=False)
    from cli.ui.commands.h_config import ConfigCtx, cmd_config
    out = cmd_config(ConfigCtx(user_id="u1", home_dir=tmp_path),
                     ["set", "CODE_EXEC_ENABLED", "true", "--global"])
    assert out.startswith("Set "), out
    assert "CODE_EXEC_ENABLED=true" in (tmp_path / "g-home" / ".env").read_text()


def test_no_writer_bypasses_the_service():
    """Ratchet: neither `config set` nor REPL `/config set` writes an env file itself."""
    import cli.commands.config as cfg
    import cli.ui.commands.h_config as hc
    for src in (inspect.getsource(cfg.set_cmd.callback), inspect.getsource(hc._cmd_set)):
        assert "_upsert_env(" not in src
        assert "upsert_env_var" not in src
        assert "set_value" in src or "_service_set" in src or "_write_env_flag" in src
    assert "upsert_env_var" not in inspect.getsource(cfg._write_env_flag)
    assert "set_value" in inspect.getsource(cfg._service_set)
