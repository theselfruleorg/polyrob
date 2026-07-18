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


# --- Task 9 / G-2: `owner pending`/`promote`/`reject tool_approval` -------------------

def _seed_tool_approval(tmp_path, uid="gleb", tool_name="x402_request"):
    from agents.task.goals.board import GoalBoard
    board = GoalBoard(str(tmp_path / "goals.db"))
    ask = board.create_ask(
        user_id=uid, what=f"Approve {tool_name}? [c0ffee01]",
        why=f"tool={tool_name} params={{}}",
        extra_payload={"ask_kind": "tool_approval", "tool_name": tool_name,
                       "request_hash": "c0ffee01", "grant_consumed": False},
        force=True,
    )
    return board, ask


def test_pending_lists_tool_approval_ask(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    board, ask = _seed_tool_approval(tmp_path)
    res = CliRunner().invoke(owner, ["pending"])
    assert res.exit_code == 0
    assert "tool_approval" in res.output
    assert f"tap-{ask.id}" in res.output


def test_promote_tool_approval_approves_request(tmp_path, monkeypatch):
    from agents.task.goals.board import ASK_FULFILLED
    _env(tmp_path, monkeypatch)
    board, ask = _seed_tool_approval(tmp_path)
    res = CliRunner().invoke(owner, ["promote", "tool_approval", f"tap-{ask.id}"])
    assert res.exit_code == 0
    assert "approved" in res.output.lower()
    assert board.get(ask.id).status == ASK_FULFILLED


def test_reject_tool_approval_declines_request(tmp_path, monkeypatch):
    from agents.task.goals.board import ASK_REJECTED
    _env(tmp_path, monkeypatch)
    board, ask = _seed_tool_approval(tmp_path)
    res = CliRunner().invoke(owner, ["reject", "tool_approval", f"tap-{ask.id}"])
    assert res.exit_code == 0
    assert "rejected" in res.output.lower()
    assert board.get(ask.id).status == ASK_REJECTED


def test_promote_tool_approval_unknown_id_exits_nonzero(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["promote", "tool_approval", "tap-nope"])
    assert res.exit_code != 0


# --- owner-UX P2-4 final review, item 4: correct kind labels ------------------

def test_pending_labels_contract_and_pref_change_correctly(tmp_path, monkeypatch):
    """`owner pending` used to hardcode a 2-kind label map (self_context vs.
    everything-else="skill"), so a contract or pref_change proposal displayed
    as a generic "skill" — wrong. Both now render with their own label via the
    shared `core.self_evolution.pending_kind_label`."""
    _env(tmp_path, monkeypatch)
    from core.contract_writer import ContractWriter
    from core.prefs import propose_pref_change

    ContractWriter(tmp_path, instance_id="rob").propose(
        "Never spend more than $5 without asking.", user_id="gleb",
        created_by="user", pending=True)
    ok, result = propose_pref_change("gleb", "approvals.require", None, tmp_path,
                                     instance_id="rob", op="remove_entry",
                                     entry="git_push")
    assert ok, result

    res = CliRunner().invoke(owner, ["pending"])
    assert res.exit_code == 0
    assert "contract" in res.output
    assert "pref change" in res.output
    assert "skill    contract:" not in res.output
    assert "skill    pref_change:" not in res.output


def test_promote_tool_approval_accepts_bare_id_without_prefix(tmp_path, monkeypatch):
    """kind='tool_approval' already disambiguates, so a bare (non-tap-) id also
    resolves — the prefix is a display/dispatch aid, not a requirement."""
    from agents.task.goals.board import ASK_FULFILLED
    _env(tmp_path, monkeypatch)
    board, ask = _seed_tool_approval(tmp_path)
    res = CliRunner().invoke(owner, ["promote", "tool_approval", ask.id])
    assert res.exit_code == 0
    assert board.get(ask.id).status == ASK_FULFILLED
