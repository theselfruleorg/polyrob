"""A re-run of `polyrob init` keeps the identity it wrote last time.

The wizard says "Press Enter to keep them": the prompt defaults must be the
values the .env already holds, not the shipped defaults. An explicit CLI
option still wins.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

_IDENTITY = {
    "POLYROB_INSTANCE_ID": "fixture-instance",
    "POLYROB_OWNER_USER_ID": "fixture-owner",
    "POLYROB_AGENT_TOOLSET": "coding",
    "POLYROB_PERSONA": "research",
}

# Section 1 (3 blanks), model, toolset, template, character confirm,
# instance id, owner id; then local, autonomy, preset, digest, surface, wallet.
_ALL_ENTER = "\n".join([""] * 9 + ["", "", "", "", "", "n"]) + "\n"


def _invoke(tmp_path, monkeypatch, args, input_text):
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True, exist_ok=True)
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    for key in _IDENTITY:
        monkeypatch.delenv(key, raising=False)
    from cli.commands.init import init_cmd
    return home, CliRunner().invoke(init_cmd, args, input=input_text)


def _seed(home: Path, values: dict) -> Path:
    env = home / ".polyrob" / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return env


def _read(env: Path) -> dict:
    return {k.strip(): v.strip() for k, v in
            (ln.split("=", 1) for ln in env.read_text().splitlines() if "=" in ln)}


def test_enter_everywhere_keeps_the_existing_identity(tmp_path, monkeypatch):
    home = tmp_path / "home"
    env = _seed(home, _IDENTITY)
    _home, res = _invoke(tmp_path, monkeypatch, [], _ALL_ENTER)
    assert res.exit_code == 0, res.output
    got = _read(env)
    for key, value in _IDENTITY.items():
        assert got.get(key) == value, (key, res.output)


def test_an_explicit_option_still_wins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    env = _seed(home, _IDENTITY)
    # --owner and --instance-id given: those two prompts are not asked.
    inp = "\n".join([""] * 7 + ["", "", "", "", "", "n"]) + "\n"
    _home, res = _invoke(tmp_path, monkeypatch,
                         ["--owner", "other-owner", "--instance-id", "other-instance"], inp)
    assert res.exit_code == 0, res.output
    got = _read(env)
    assert got["POLYROB_OWNER_USER_ID"] == "other-owner"
    assert got["POLYROB_INSTANCE_ID"] == "other-instance"
    assert got["POLYROB_AGENT_TOOLSET"] == "coding"

