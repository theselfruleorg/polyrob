import json

import pytest

from core.maintenance_proposals import MaintenanceProposalStore


def test_source_proposal_binds_exact_base_and_candidate_bytes(tmp_path):
    store = MaintenanceProposalStore(tmp_path / "proposals")
    proposal = store.propose_source_patch(path="agents/example.py", base_content="VALUE = 1\n",
                                          candidate_content="VALUE = 2\n", user_id="u1", session_id="s1")
    assert proposal["base_sha256"] != proposal["candidate_sha256"]
    loaded = store.load(proposal["proposal_id"])
    assert loaded["candidate_content"] == "VALUE = 2\n"
    assert loaded["activation"] == "trusted_cli_or_supervisor_only"


@pytest.mark.parametrize("path", ["../escape.py", "/etc/passwd", "a\\b.py", "a/../../b.py"])
def test_proposals_refuse_unsafe_paths(tmp_path, path):
    with pytest.raises(ValueError):
        MaintenanceProposalStore(tmp_path).propose_source_patch(
            path=path, base_content="a", candidate_content="b")


def test_proposal_tampering_is_detected(tmp_path):
    store = MaintenanceProposalStore(tmp_path)
    proposal = store.propose_dependency(package="package==1.2.3")
    path = tmp_path / f"{proposal['proposal_id']}.json"
    record = json.loads(path.read_text())
    record["requirement"] = "package==999"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="altered"):
        store.load(proposal["proposal_id"])


@pytest.mark.parametrize("requirement", ["package", "package>=1", "-rrequirements.txt"])
def test_dependency_requires_exact_pin(tmp_path, requirement):
    with pytest.raises(ValueError):
        MaintenanceProposalStore(tmp_path).propose_dependency(package=requirement)
