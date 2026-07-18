"""R-1: env_file_candidates() is the ONE canonical .env precedence list.

The contract test: the helper's order IS load_env's actual resolution order —
write a conflicting probe key into every candidate file, then peel files off the
front and assert the winner is always the helper's first existing candidate.
"""
import os
from pathlib import Path

PROBE = "POLYROB_TEST_PRECEDENCE_PROBE"


def _make_local_layout(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    (home / ".polyrob").mkdir(parents=True)
    (home / ".rob").mkdir(parents=True)
    (proj / ".polyrob").mkdir(parents=True)
    (proj / "config").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("POLYROB_HOME", raising=False)
    monkeypatch.chdir(proj)
    return home, proj


def test_local_candidate_order_and_tiers(tmp_path, monkeypatch):
    home, proj = _make_local_layout(tmp_path, monkeypatch)
    from core.paths import env_file_candidates
    cands = env_file_candidates("development", local_mode=True)
    assert [c.tier for c in cands] == [
        "project", "home", "legacy-home", "root", "config-env", "config-env-local"]
    assert cands[0].path == proj / ".polyrob" / ".env"
    assert cands[1].path == home / ".polyrob" / ".env"
    assert cands[2].path == home / ".rob" / ".env"


def test_server_candidate_order_and_tiers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from core.paths import env_file_candidates
    cands = env_file_candidates("production", local_mode=False)
    assert [c.tier for c in cands] == ["config-env-local", "config-env", "root"]
    assert cands[0].path == Path("config") / ".env.production.local"


def test_helper_order_is_load_env_resolution_local(tmp_path, monkeypatch):
    """Peel candidates off the front: the winner is always the first existing file."""
    _make_local_layout(tmp_path, monkeypatch)
    from core.paths import env_file_candidates
    cands = env_file_candidates("development", local_mode=True)
    for c in cands:
        c.path.parent.mkdir(parents=True, exist_ok=True)
        c.path.write_text(f"{PROBE}={c.tier}\n")
    from core.bootstrap import load_env
    for i, expected in enumerate(cands):
        monkeypatch.delenv(PROBE, raising=False)
        load_env("development", local_mode=True)
        assert os.environ[PROBE] == expected.tier, (
            f"round {i}: expected tier {expected.tier!r} to win")
        expected.path.unlink()
    monkeypatch.delenv(PROBE, raising=False)


def test_helper_order_is_load_env_resolution_server(tmp_path, monkeypatch):
    _make_local_layout(tmp_path, monkeypatch)  # provides config/ + cwd isolation
    from core.paths import env_file_candidates
    cands = env_file_candidates("development", local_mode=False)
    for c in cands:
        c.path.parent.mkdir(parents=True, exist_ok=True)
        c.path.write_text(f"{PROBE}={c.tier}\n")
    from core.bootstrap import load_env
    for i, expected in enumerate(cands):
        monkeypatch.delenv(PROBE, raising=False)
        load_env("development", local_mode=False)
        assert os.environ[PROBE] == expected.tier, (
            f"round {i}: expected tier {expected.tier!r} to win")
        expected.path.unlink()
    monkeypatch.delenv(PROBE, raising=False)


def test_update_context_captures_home_and_legacy_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    (home / ".rob").mkdir(parents=True)
    (home / ".polyrob" / ".env").write_text("A=1\n")
    (home / ".rob" / ".env").write_text("B=2\n")
    proj = tmp_path / "proj"; proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("POLYROB_HOME", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.chdir(proj)
    from cli.update.context import resolve_update_context
    ctx = resolve_update_context(local=True)
    assert home / ".polyrob" / ".env" in ctx.config_paths
    assert home / ".rob" / ".env" in ctx.config_paths
