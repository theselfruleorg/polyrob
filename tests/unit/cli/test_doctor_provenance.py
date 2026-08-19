"""W1.3 (HANDOFF-env-system-and-key-subscriptions-2026-08-14): doctor names the
SOURCE FILE tier on every env-backed credential line, so a shadowed key
(§1.2.3: a malformed value in a higher tier masking a real one below) is
visible in one read instead of three greps. Values are never printed.
"""
from pathlib import Path

from cli.commands.doctor import doctor_report


def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    return home, proj


def test_present_line_names_the_home_file_tier(tmp_path, monkeypatch):
    home, _ = _isolated(tmp_path, monkeypatch)
    key = "sk-zai-0123456789abcdefgh"
    (home / ".polyrob" / ".env").write_text(f"ZAI_API_KEY={key}\n")
    lines = doctor_report({"ZAI_API_KEY": key})
    line = next(ln for ln in lines if ln.strip().startswith("zai-coding:"))
    assert "~/.polyrob/.env" in line
    assert key not in line


def test_present_line_names_the_project_file_tier(tmp_path, monkeypatch):
    _, proj = _isolated(tmp_path, monkeypatch)
    (proj / ".polyrob").mkdir()
    key = "sk-ant-0123456789abcdefgh"
    (proj / ".polyrob" / ".env").write_text(f"ANTHROPIC_API_KEY={key}\n")
    lines = doctor_report({"ANTHROPIC_API_KEY": key})
    line = next(ln for ln in lines if ln.strip().startswith("anthropic:"))
    assert "./.polyrob/.env" in line


def test_process_env_override_is_named(tmp_path, monkeypatch):
    # The file holds a DIFFERENT value than the process env — with load_env's
    # override=False semantics that means the process env preempted the file.
    home, _ = _isolated(tmp_path, monkeypatch)
    (home / ".polyrob" / ".env").write_text("ZAI_API_KEY=sk-zai-file-0123456789\n")
    lines = doctor_report({"ZAI_API_KEY": "sk-zai-process-0123456789"})
    line = next(ln for ln in lines if ln.strip().startswith("zai-coding:"))
    assert "process env" in line
    assert "~/.polyrob/.env" in line          # ...and what it overrides


def test_no_file_means_process_env(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    lines = doctor_report({"ZAI_API_KEY": "sk-zai-0123456789abcdefgh"})
    line = next(ln for ln in lines if ln.strip().startswith("zai-coding:"))
    assert "process env" in line


def test_malformed_line_names_tier_and_global_scope(tmp_path, monkeypatch):
    # §1.2.3 exactly: a malformed placeholder in ~/.polyrob/.env. The line must
    # say WHERE it is and the remedy must carry the matching --global scope.
    home, _ = _isolated(tmp_path, monkeypatch)
    (home / ".polyrob" / ".env").write_text("ANTHROPIC_API_KEY=x\n")
    lines = doctor_report({"ANTHROPIC_API_KEY": "x"})
    line = next(ln for ln in lines if ln.strip().startswith("anthropic:"))
    assert "~/.polyrob/.env" in line
    assert "polyrob config unset ANTHROPIC_API_KEY --global" in line


def test_malformed_in_a_non_unset_tier_names_the_file(tmp_path, monkeypatch):
    # `config unset` only manages the two .polyrob files — for a legacy tier
    # (root .env) the honest remedy is the file itself, not a verb that will miss.
    _, proj = _isolated(tmp_path, monkeypatch)
    (proj / ".env").write_text("ANTHROPIC_API_KEY=x\n")
    lines = doctor_report({"ANTHROPIC_API_KEY": "x"})
    line = next(ln for ln in lines if ln.strip().startswith("anthropic:"))
    assert ".env" in line
    assert "remove it from" in line
