"""113 §2 item 5 (I-2): `polyrob owner approve <id>` decides ANY pending item, like
chat `/approve <id>`; the person form is `owner person-approve`, and the old
`approve <surface> <address>` / `approve --all` still run it."""
from types import SimpleNamespace

import pytest
from click.testing import CliRunner


def _runner():
    """Split-stream runner across click versions: 8.1 needs mix_stderr=False
    for a separate .stderr; 8.2+ removed the kwarg and always splits."""
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:  # click >= 8.2
        return CliRunner()


@pytest.fixture
def rig(monkeypatch, tmp_path):
    from cli.commands import owner_decide as od
    monkeypatch.setattr("polyrob.app.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.setattr(od, "ensure_env_loaded", lambda: None)
    monkeypatch.setattr(od, "_owner_tenant", lambda user: user or "tenant-a")
    monkeypatch.setattr(od, "_data_dir", lambda write=None: str(tmp_path))
    monkeypatch.setattr(od, "_instance_id", lambda: "inst")
    monkeypatch.setattr(od, "_goal_board", lambda: None)
    monkeypatch.setattr(od, "_registry", lambda home: None)
    items = [{"kind": "skill", "id": "draft-1"}, {"kind": "tool_approval", "id": "tap-9"}]
    pending = SimpleNamespace(items=items, unavailable=[], degraded_line=lambda: "")
    monkeypatch.setattr("tools.controller.approval_queue.all_pending", lambda **k: pending)
    calls = []

    def _decide(kind, item_id, **kw):
        calls.append((kind, item_id, kw["approve"], kw["user_id"]))
        return True, f"approved {kind}:{item_id}"

    monkeypatch.setattr("tools.controller.approval_queue.decide_pending", _decide)
    return calls


def _owner(*args):
    from cli.commands.owner import owner
    return _runner().invoke(owner, list(args))


def test_approve_by_id_decides_that_item(rig):
    res = _owner("approve", "draft-1")
    assert res.exit_code == 0, res.output
    assert rig == [("skill", "draft-1", True, "tenant-a")]
    assert "approved skill:draft-1" in res.output


def test_approve_takes_kind_colon_id(rig):
    assert _owner("approve", "tool_approval:tap-9").exit_code == 0
    assert rig == [("tool_approval", "tap-9", True, "tenant-a")]


def test_an_unknown_id_is_refused_and_decides_nothing(rig):
    res = _owner("approve", "nope")
    assert res.exit_code == 1
    assert "no pending item 'nope'" in res.output and rig == []


def test_approve_all_decides_the_whole_union(monkeypatch, rig):
    from cli.commands import owner_decide as od
    seen = []
    monkeypatch.setattr(od, "_decide_all_and_echo", lambda approve, tenant: seen.append((approve, tenant)))
    assert _owner("approve", "all").exit_code == 0
    assert seen == [(True, "tenant-a")]


def test_bare_approve_is_a_usage_line(rig):
    res = _owner("approve")
    assert res.exit_code == 1 and "owner pending" in res.output and rig == []


def test_the_person_form_still_runs_under_both_names(monkeypatch, rig):
    from cli.commands import owner as owner_mod
    approved = []

    class _Reg:
        def approve(self, **kw):
            approved.append(kw)
            return True

    monkeypatch.setattr(owner_mod, "_registry", lambda home: _Reg())
    monkeypatch.setattr(owner_mod, "_data_dir", lambda write=None: "/unused")
    monkeypatch.setattr(owner_mod, "_owner_tenant", lambda user: "tenant-a")
    res = _owner("approve", "telegram", "someone")
    assert res.exit_code == 0, res.output
    assert "person-approve" in res.stderr
    res = _owner("person-approve", "telegram", "someone")
    assert res.exit_code == 0, res.output
    assert [a["surface"] for a in approved] == ["telegram", "telegram"]
    assert rig == []  # the item decider never ran
