"""080 WS-1 item 5 — ignored posture-member env lines are a /status health item."""
from core.config_policy.members import ignored_member_health


def test_clean_env_has_no_item():
    assert ignored_member_health({}) is None


def test_direct_member_lines_are_counted_and_named():
    env = {"SHELL_TOOLS_ENABLED": "false", "CODING_TOOLS_ENABLED": "true"}
    key, text, remedy = ignored_member_health(env)
    assert key == "member_env_lines_ignored"
    assert text.startswith("2 lines in polyrob.env are ignored (posture members)")
    assert "delete them or move them into POLYROB_OVERRIDES" in text
    assert "POLYROB_OVERRIDES=" in remedy


def test_status_facts_carries_the_item(tmp_path, monkeypatch):
    from core import lifecycle as lc
    from core.config_policy.members import MEMBER_FLAGS
    name = sorted(MEMBER_FLAGS)[0]
    import os
    monkeypatch.setitem(os.environ, name, "true")  # a DIRECT line (conftest shim bypass)
    facts = lc.status_facts(str(tmp_path / "data"))
    assert any(k == "member_env_lines_ignored" for k, _t, _r in facts["health"])
