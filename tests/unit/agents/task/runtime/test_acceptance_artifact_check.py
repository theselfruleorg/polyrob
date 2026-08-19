"""An `artifact` acceptance check asks the ledger, not the filesystem layout.

Five goals failed last week with variants of

    acceptance checks failed: file_contains: file not found
      ('data/x402-round4-decode.md')

The agent HAD written that evidence. `file_contains` resolves a relative path
against a workspace that, on the shared project root, had since been wiped — so
a check meant to verify the work instead reported the work was never done. The
run was then recorded as a failure and retried from zero.

Keying the check on the artifact ledger separates the three states the path
check conflates: never produced (`unknown`), produced then deleted (`missing`),
and produced then altered (`changed`).
"""
import asyncio

import pytest

from core.artifacts import get_artifact_ledger
from agents.task.runtime.acceptance_checks import run_acceptance_checks


def _run(checks, **ctx):
    return asyncio.run(run_acceptance_checks(checks, **ctx))


@pytest.fixture
def evidence(tmp_path):
    p = tmp_path / "x402-round4-decode.md"
    p.write_text("decoded the 402 challenge: accepts[] present")
    get_artifact_ledger().record("rob", str(p), session_id="s1", goal_id="g1",
                                 kind="report")
    return p


def test_artifact_check_passes_by_name_for_the_goals_own_output(evidence):
    res = _run([{"type": "artifact", "name": "x402-round4-decode.md"}],
               user_id="rob", goal_id="g1")

    assert res[0]["ok"] is True
    assert "ok" in res[0]["detail"]


def test_a_deleted_artifact_reports_missing_not_never_produced(evidence):
    evidence.unlink()

    res = _run([{"type": "artifact", "name": "x402-round4-decode.md"}],
               user_id="rob", goal_id="g1")

    assert res[0]["ok"] is False
    assert "missing" in res[0]["detail"], (
        "a wiped artifact must not read as 'the agent never produced it'")


def test_an_altered_artifact_reports_changed(evidence):
    evidence.write_text("something else entirely")

    res = _run([{"type": "artifact", "name": "x402-round4-decode.md"}],
               user_id="rob", goal_id="g1")

    assert res[0]["ok"] is False
    assert "changed" in res[0]["detail"]


def test_an_artifact_never_produced_is_reported_as_such(evidence):
    res = _run([{"type": "artifact", "name": "never-written.md"}],
               user_id="rob", goal_id="g1")

    assert res[0]["ok"] is False
    assert "no artifact" in res[0]["detail"]


def test_contains_still_works_on_top_of_the_ledger_resolution(evidence):
    ok = _run([{"type": "artifact", "name": "x402-round4-decode.md",
                "contains": ["accepts[]"]}], user_id="rob", goal_id="g1")
    assert ok[0]["ok"] is True

    bad = _run([{"type": "artifact", "name": "x402-round4-decode.md",
                 "contains": ["a phrase that is not there"]}],
               user_id="rob", goal_id="g1")
    assert bad[0]["ok"] is False
    assert "contains" in bad[0]["detail"]


def test_an_artifact_id_can_be_named_directly(evidence, tmp_path):
    row = get_artifact_ledger().list_for_goal("rob", "g1")[0]

    res = _run([{"type": "artifact", "id": row.id}], user_id="rob", goal_id="g1")

    assert res[0]["ok"] is True


def test_another_tenants_artifact_never_satisfies_a_check(evidence):
    res = _run([{"type": "artifact", "name": "x402-round4-decode.md"}],
               user_id="someone_else", goal_id="g1")

    assert res[0]["ok"] is False
