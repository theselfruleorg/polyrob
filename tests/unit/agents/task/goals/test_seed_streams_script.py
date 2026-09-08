"""One failing stream must not take the rest of the manifest down with it.

`scripts/seed_streams.py` iterated the declared streams with no per-stream
guard, so the FIRST stream that raised aborted the run and every LATER stream
was silently never seeded. Latent while one stream is declared; live the moment
a second is added, which is the whole point of the manifest.
"""
import importlib.util
from pathlib import Path

import pytest
import yaml

from agents.task.goals import streams as S
from agents.task.goals.board import GoalBoard

ROOT = Path(__file__).resolve().parents[5]

# `scripts/` is operator/deploy tooling and never ships in the public framework
# export, so these tests have nothing to exercise there. Skip honestly rather
# than fail: on the private tree, where the script exists, they always run.
SEEDER = ROOT / "scripts" / "seed_streams.py"
pytestmark = pytest.mark.skipif(
    not SEEDER.is_file(),
    reason="scripts/seed_streams.py is operator tooling, absent from the public tree",
)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "seed_streams_script", ROOT / "scripts" / "seed_streams.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stream(sid: str) -> dict:
    return {"id": sid, "cadence_hours": 4,
            "objective": {"title": f"{sid} objective", "priority": 1},
            "goals": [{"title": f"{sid} leg", "body": "do the work",
                       "priority": 2, "tools": ["filesystem", "task"]}]}


@pytest.fixture
def manifest(tmp_path):
    p = tmp_path / "streams.yaml"
    p.write_text(yaml.safe_dump(
        {"version": 1, "streams": [_stream("alpha"), _stream("bravo")]}),
        encoding="utf-8")
    return p


def test_a_failing_stream_does_not_stop_the_later_ones(tmp_path, manifest,
                                                       monkeypatch, capsys):
    script = _load_script()
    db = str(tmp_path / "goals.db")
    real_seed = S.seed_stream

    def selective(board, user_id, stream, objective_id):
        if stream["id"] == "alpha":
            raise RuntimeError("alpha is broken")
        return real_seed(board, user_id, stream, objective_id)

    monkeypatch.setattr(S, "seed_stream", selective)
    rc = script.main(["--db", db, "--user-id", "rob", "--manifest", str(manifest)])

    assert rc == 1, "a failed stream must make the unit fail, not exit 0"
    board = GoalBoard(db)
    assert S.stream_live_goals(board, "rob", "bravo") == 1, \
        "bravo was never seeded — one stream's failure took down the run"
    assert S.stream_live_goals(board, "rob", "alpha") == 0
    err = capsys.readouterr().err
    assert "alpha" in err and "bravo" not in err


def test_a_failing_stream_files_an_owner_ask(tmp_path, manifest, monkeypatch):
    script = _load_script()
    db = str(tmp_path / "goals.db")
    monkeypatch.setattr(S, "seed_stream", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("everything is broken")))
    script.main(["--db", db, "--user-id", "rob", "--manifest", str(manifest)])

    board = GoalBoard(db)
    streams_named = {(a.payload or {}).get("stream")
                     for a in board.asks(user_id="rob", status="open")}
    assert streams_named == {"alpha", "bravo"}


def test_a_missing_manifest_exits_2_and_files_an_ask(tmp_path):
    script = _load_script()
    db = str(tmp_path / "goals.db")
    rc = script.main(["--db", db, "--user-id", "rob",
                      "--manifest", str(tmp_path / "nope.yaml")])
    assert rc == 2
    board = GoalBoard(db)
    asks = board.asks(user_id="rob", status="open")
    assert [(a.payload or {}).get("stream") for a in asks] == ["__manifest__"]


def test_a_clean_run_exits_0_and_leaves_no_ask(tmp_path, manifest):
    script = _load_script()
    db = str(tmp_path / "goals.db")
    assert script.main(["--db", db, "--user-id", "rob",
                        "--manifest", str(manifest)]) == 0
    board = GoalBoard(db)
    assert board.asks(user_id="rob", status="open") == []
    assert S.stream_live_goals(board, "rob", "alpha") == 1
    assert S.stream_live_goals(board, "rob", "bravo") == 1
