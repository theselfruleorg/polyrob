"""`polyrob owner pending/promote/reject` — the owner-facing self-evolution surface (§7.1)."""
import os

from click.testing import CliRunner

from cli.commands.owner import owner
from core.self_context_writer import SelfContextWriter, PROVENANCE_AGENT


def _seed_pending_self(home, uid):
    SelfContextWriter(home, instance_id="rob").propose(
        "Learned: surface blockers to the owner proactively.",
        user_id=uid, created_by=PROVENANCE_AGENT, pending=True)


def _env(tmp_path, monkeypatch):
    # Point the owner CLI's data home + owner tenant at an isolated tmp dir.
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "gleb")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")


def test_pending_empty(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["pending"])
    assert res.exit_code == 0
    assert "no pending" in res.output


def test_pending_lists_self_context(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _seed_pending_self(tmp_path, "gleb")
    res = CliRunner().invoke(owner, ["pending"])
    assert res.exit_code == 0
    assert "self_context" in res.output
    assert "surface blockers" in res.output


def test_promote_self_context(tmp_path, monkeypatch):
    from core.instance import load_self_doc
    _env(tmp_path, monkeypatch)
    _seed_pending_self(tmp_path, "gleb")
    res = CliRunner().invoke(owner, ["promote", "self_context", "gleb"])
    assert res.exit_code == 0
    assert "surface blockers" in load_self_doc(tmp_path, user_id="gleb")


def test_reject_self_context(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _seed_pending_self(tmp_path, "gleb")
    res = CliRunner().invoke(owner, ["reject", "self_context", "gleb"])
    assert res.exit_code == 0
    # now nothing pending
    res2 = CliRunner().invoke(owner, ["pending"])
    assert "no pending" in res2.output


def test_promote_unknown_kind_exits_nonzero(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["promote", "bogus", "x"])
    assert res.exit_code != 0
