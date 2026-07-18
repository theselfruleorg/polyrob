"""Tests for `polyrob soul init` (cli/commands/soul.py)."""
from click.testing import CliRunner
from cli.commands.soul import soul


def test_soul_init_scaffolds_docs(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.soul._data_home", lambda: tmp_path)
    monkeypatch.setattr("click.edit", lambda *a, **k: None)   # no editor in tests
    runner = CliRunner()
    result = runner.invoke(soul, ["init", "--no-edit"], input="Robbie\nhelp my owner ship\n")
    assert result.exit_code == 0, result.output
    identity = (tmp_path / "identity" / "identity.md").read_text()
    operating = (tmp_path / "identity" / "operating.md").read_text()
    assert "Robbie" in identity
    assert operating.strip()


def test_soul_init_refuses_overwrite(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.soul._data_home", lambda: tmp_path)
    (tmp_path / "identity").mkdir(parents=True)
    (tmp_path / "identity" / "identity.md").write_text("existing")
    runner = CliRunner()
    result = runner.invoke(soul, ["init", "--no-edit"], input="x\ny\n")
    assert result.exit_code != 0
    assert "exists" in result.output.lower()
    assert (tmp_path / "identity" / "identity.md").read_text() == "existing"


def test_soul_init_force_overwrites(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.soul._data_home", lambda: tmp_path)
    (tmp_path / "identity").mkdir(parents=True)
    (tmp_path / "identity" / "identity.md").write_text("existing")
    runner = CliRunner()
    result = runner.invoke(soul, ["init", "--no-edit", "--force"], input="NewName\npurpose\n")
    assert result.exit_code == 0, result.output
    assert "NewName" in (tmp_path / "identity" / "identity.md").read_text()
