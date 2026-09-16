import json
import hashlib

import pytest

from core.eval_manifest import EvalRun, digest


def make_run(tmp_path, model="fixture"):
    return EvalRun.create(tmp_path, scenarios=[{"id": "case", "assertions": ["artifact", "test"]}],
                          configuration={"model": model}, revision={"commit": "fixture"})


def evidence(run, text="independent verifier receipt"):
    path = run.directory / "proof.txt"
    path.write_text(text)
    return [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]


def finish(run, code=0):
    run.finalize(runner_exit_code=code, revision_after=run.manifest["revision"])


def test_interleaved_models_cannot_mix_results(tmp_path):
    first, second = make_run(tmp_path, "a"), make_run(tmp_path, "b")
    first.record("case", assertions={"artifact": True, "test": True}, evidence=evidence(first))
    finish(first)
    assert first.score()["passed"]
    assert not second.score()["passed"]
    (second.directory / "case.json").write_bytes((first.directory / "case.json").read_bytes())
    assert second.score()["results"][0]["outcome"] == "error"


def test_missing_and_partial_assertions_never_pass(tmp_path):
    run = make_run(tmp_path)
    assert run.score()["exit_code"] == 1
    with pytest.raises(ValueError):
        run.record("case", assertions={"artifact": True}, evidence=evidence(run))


def test_keywords_are_not_verification(tmp_path):
    run = make_run(tmp_path)
    run.record("case", assertions={"artifact": True, "test": False},
               evidence=evidence(run, "fizzbuzz tests never ran; none passed"))
    finish(run)
    assert run.score()["results"][0]["outcome"] == "failed"


def test_quoted_false_denial_does_not_override_verifier(tmp_path):
    run = make_run(tmp_path)
    run.record("case", assertions={"artifact": True, "test": True},
               evidence=evidence(run, "refutation of 'does not exist'"))
    finish(run)
    assert run.score()["passed"]


def test_records_are_exclusive_and_manifests_are_sealed(tmp_path):
    run = make_run(tmp_path)
    kwargs = dict(assertions={"artifact": True, "test": True}, evidence=evidence(run))
    run.record("case", **kwargs)
    with pytest.raises(FileExistsError):
        run.record("case", **kwargs)
    manifest = run.manifest.copy()
    manifest["configuration"] = {"model": "different"}
    (run.directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        EvalRun(run.directory)


@pytest.mark.parametrize("budget", [-1, float("nan"), float("inf"), 1, True, "0"])
def test_fixture_budget_is_zero(tmp_path, budget):
    with pytest.raises(ValueError):
        EvalRun.create(tmp_path, scenarios=[{"id": "case", "assertions": ["test"]}],
                       configuration={}, revision={}, budget_usd=budget)


def test_required_runner_completion_survives_rescoring(tmp_path):
    run = make_run(tmp_path)
    run.record("case", assertions={"artifact": True, "test": True}, evidence=evidence(run))
    assert not run.score()["passed"]
    finish(run, code=3)
    assert not EvalRun(run.directory).score()["passed"]
    with pytest.raises(FileExistsError):
        finish(run)


def test_changed_source_cannot_pass(tmp_path):
    run = make_run(tmp_path)
    run.record("case", assertions={"artifact": True, "test": True}, evidence=evidence(run))
    run.finalize(runner_exit_code=0, revision_after={"commit": "different"})
    assert "source changed" in run.score()["runner_error"]


def test_artifact_digest_is_rechecked_when_rescoring(tmp_path):
    run = make_run(tmp_path)
    run.record("case", assertions={"artifact": True, "test": True}, evidence=evidence(run))
    finish(run)
    assert run.score()["passed"]
    (run.directory / "proof.txt").write_text("replacement evidence")
    assert not EvalRun(run.directory).score()["passed"]


@pytest.mark.parametrize("value", ["a claimed success", ["a claimed success"], [],
                                  [{"path": "../escape", "sha256": "0" * 64}],
                                  [{"path": "/tmp/escape", "sha256": "0" * 64}]])
def test_unbacked_or_escaping_evidence_is_refused(tmp_path, value):
    run = make_run(tmp_path)
    with pytest.raises(ValueError):
        run.record("case", assertions={"artifact": True, "test": True}, evidence=value)


def test_symlink_evidence_is_refused(tmp_path):
    run = make_run(tmp_path)
    proof = evidence(run)
    path = run.directory / "proof.txt"
    path.rename(tmp_path / "outside.txt")
    path.symlink_to(tmp_path / "outside.txt")
    with pytest.raises(OSError):
        run.record("case", assertions={"artifact": True, "test": True}, evidence=proof)


@pytest.mark.parametrize("change", [{"scenarios": []}, {"budget_usd": -1},
                                   {"mode": "anything"}, {"configuration": []}])
def test_rehashed_but_malformed_manifest_is_refused(tmp_path, change):
    run = make_run(tmp_path)
    manifest = {**run.manifest, **change}
    manifest.pop("digest")
    (run.directory / "manifest.json").write_text(json.dumps({**manifest, "digest": digest(manifest)}))
    with pytest.raises(ValueError):
        EvalRun(run.directory)


def test_public_manifest_snapshot_cannot_change_scoring(tmp_path):
    run = make_run(tmp_path)
    run.manifest["scenarios"].clear()
    assert not run.score()["passed"]


def test_manifest_change_after_open_is_refused(tmp_path):
    run = make_run(tmp_path)
    manifest = run.manifest
    manifest.pop("digest")
    manifest["configuration"]["model"] = "other"
    (run.directory / "manifest.json").write_text(json.dumps({**manifest, "digest": digest(manifest)}))
    with pytest.raises(ValueError):
        run.score()


def test_records_cannot_be_added_after_finalization(tmp_path):
    run = make_run(tmp_path)
    proof = evidence(run)
    finish(run)
    with pytest.raises(ValueError, match="finalized"):
        run.record("case", assertions={"artifact": True, "test": True}, evidence=proof)


def test_live_records_need_exact_goal_and_session(tmp_path):
    run = EvalRun.create(tmp_path, mode="live", budget_usd=1,
                         scenarios=[{"id": "case", "goal_id": "goal-1", "assertions": ["test"]}],
                         configuration={"model": "fixture"}, revision={})
    kwargs = dict(assertions={"test": True}, evidence=evidence(run))
    with pytest.raises(ValueError):
        run.record("case", goal_id="wrong", session_id="session-1", **kwargs)
    with pytest.raises(ValueError):
        run.record("case", goal_id="goal-1", **kwargs)
    run.record("case", goal_id="goal-1", session_id="session-1", spend_usd=2, **kwargs)
    finish(run)
    assert not run.score()["within_budget"]


def test_exclusive_write_rejects_replaced_run_directory(tmp_path):
    from core.eval_manifest import _exclusive_json
    run = make_run(tmp_path)
    moved = run.directory.with_name("saved")
    run.directory.rename(moved)
    run.directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        _exclusive_json(run.directory / "case.json", {"result": "untrusted"})
    assert not (moved / "case.json").exists()
