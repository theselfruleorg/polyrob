"""No live seeding, implicit DB creation, or heuristic release success."""
import pytest

from scripts.eval import run_eval


def test_seed_refuses_before_creating_database(tmp_path, capsys):
    db = tmp_path / "goals.db"
    assert run_eval.main(["--seed", "--db", str(db)]) == 2
    assert not db.exists()
    assert "disabled" in capsys.readouterr().err
    assert not hasattr(run_eval, "_legacy_seed_unsafe")


def test_score_requires_exact_run(tmp_path, capsys):
    assert run_eval.main(["--score"]) == 2
    assert "--manifest" in capsys.readouterr().err


def test_legacy_diagnostic_cannot_pass_or_create_db(tmp_path):
    db = tmp_path / "absent.db"
    assert run_eval.main(["--legacy-score", "--db", str(db)]) == 2
    assert not db.exists()


def test_default_uses_fixture_runner(monkeypatch, tmp_path):
    from scripts.eval import fixture_runner
    calls = []
    monkeypatch.setattr(fixture_runner, "run_fixtures", lambda path: calls.append(path) or 0)
    assert run_eval.main(["--output-dir", str(tmp_path)]) == 0
    assert calls == [str(tmp_path)]


def test_modes_cannot_be_combined():
    with pytest.raises(SystemExit) as exc:
        run_eval.main(["--seed", "--fixture"])
    assert exc.value.code == 2


def test_fixture_guard_blocks_io_and_env_loading():
    import dotenv
    import socket
    import subprocess
    from scripts.eval import fixture_guard
    original = socket.socket.connect
    try:
        fixture_guard.pytest_load_initial_conftests(None, None, [])
        with pytest.raises(RuntimeError):
            socket.create_connection(("example.com", 443))
        with pytest.raises(RuntimeError):
            subprocess.Popen(["should-never-execute"])
        assert dotenv.load_dotenv("must-not-read") is False
        assert dotenv.dotenv_values("must-not-read") == {}
    finally:
        fixture_guard.pytest_unconfigure(None)
    assert socket.socket.connect is original
