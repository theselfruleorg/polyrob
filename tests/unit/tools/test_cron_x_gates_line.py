"""A job's result says what will really stop its X posts (prod 2026-11-06: the agent told
the owner that "X may hold" an agent-made job, a state that does not exist)."""
import tools.cronjob_tools as ct


def _carry(monkeypatch, ids):
    monkeypatch.setattr(ct, "resolve_cron_tools", lambda payload: ids)
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)


def test_a_job_with_no_x_tool_prints_nothing(monkeypatch):
    _carry(monkeypatch, ["filesystem"])
    assert ct._x_gates_line({}) == ""


def test_an_x_job_names_no_tap_cooldown_and_the_unenforced_text(monkeypatch):
    _carry(monkeypatch, ["twitter"])
    monkeypatch.setenv("TWITTER_POST_COOLDOWN_SEC", "1800")
    monkeypatch.setattr("core.autonomy_control.allows",
                        lambda kind: type("D", (), {"allowed": True, "reason": "running"})())
    line = ct._x_gates_line({}, "u1")
    assert "no owner tap" in line and "1800s" in line
    assert "no post spacing" in line and "no code enforces it" in line
    assert "NOT POSTING" not in line


def test_spacing_and_a_pause_are_stated(monkeypatch):
    _carry(monkeypatch, ["x_browser"])
    monkeypatch.setattr("core.autonomy_control.allows",
                        lambda kind: type("D", (), {"allowed": False, "reason": "paused: social"})())
    line = ct._x_gates_line({"post_spacing_hours": 4}, "u1")
    assert line.startswith("gates: NOT POSTING NOW: paused: social")
    assert "no cooldown and no spacing" in line and "at most once" not in line


def test_spacing_is_stated_on_the_api_rail(monkeypatch):
    _carry(monkeypatch, ["twitter"])
    assert "at most once per 4 h" in ct._x_gates_line({"post_spacing_hours": 4}, "u1")


def test_deny_and_another_tenant_are_stated(monkeypatch):
    _carry(monkeypatch, ["twitter"])
    assert "no owner tap" in ct._x_gates_line({}, "u1")
    assert "not the owner's" in ct._x_gates_line({}, "someone-else")
    monkeypatch.setenv("APPROVAL_PROVIDER", "deny")
    line = ct._x_gates_line({}, "u1")
    assert "APPROVAL_PROVIDER=deny" in line and "no owner tap" not in line


def test_a_tool_fault_prints_nothing(monkeypatch):
    def _boom(payload):
        raise ValueError("bad rig")
    monkeypatch.setattr(ct, "resolve_cron_tools", _boom)
    assert ct._x_gates_line({}) == ""
