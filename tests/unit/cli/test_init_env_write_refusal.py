"""`polyrob init`: a value the env writer refuses (CR/LF) is a plain refusal, not a traceback."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner


def test_a_refused_value_is_a_plain_refusal_not_a_traceback(tmp_path, monkeypatch):
    import cli.commands.init as init_mod

    def _refuse(env_path, updates):
        raise ValueError("refused: a value holds a line break")

    monkeypatch.setattr(init_mod, "_write_env", _refuse)
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    res = CliRunner().invoke(init_mod.init_cmd, ["--no-prompt"])
    assert res.exception is None or isinstance(res.exception, SystemExit), res.output
    assert "refused: a value holds a line break" in res.output
