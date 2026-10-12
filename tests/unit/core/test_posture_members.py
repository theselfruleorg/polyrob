"""D7 (owner decision 2026-10-10, "A + one override"): a posture is the ONLY
source of its member flags; ``POLYROB_OVERRIDES=FLAG=value,...`` changes one
member; a direct env line of a member name is ignored and reported, never
refused.

⚠️ The root conftest routes ``monkeypatch.setenv(<member>, v)`` into the
override line. A test that must write a DIRECT member line uses
``monkeypatch.setitem(os.environ, NAME, v)``.
"""
import os

import pytest

from core.config_policy import members as m


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("POLYROB_LOCAL", "AUTONOMY_ENABLED", "AUTONOMY_MODE", "AUTONOMY_POSTURE",
                 "AGENT_BUILDER_MODE", m.OVERRIDES_ENV):
        monkeypatch.delenv(name, raising=False)
    for name in m.MEMBER_FLAGS:
        monkeypatch.delitem(os.environ, name, raising=False)


def test_bundle_on_turns_member_on(monkeypatch):
    assert m.member_flag("KB_ENABLED") is False
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert m.member_flag("KB_ENABLED") is True
    assert m.member_source("KB_ENABLED") == "POLYROB_LOCAL"


def test_override_off_wins_over_bundle(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setitem(os.environ, m.OVERRIDES_ENV, "KB_ENABLED=false, GIT_TOOLS_ENABLED=off")
    assert m.member_flag("KB_ENABLED") is False
    assert m.member_flag("GIT_TOOLS_ENABLED") is False
    assert m.member_flag("CODING_TOOLS_ENABLED") is True   # untouched member: bundle
    assert m.member_source("KB_ENABLED") == m.OVERRIDES_ENV


def test_override_on_without_bundle(monkeypatch):
    monkeypatch.setitem(os.environ, m.OVERRIDES_ENV, "GOALS_ENABLED=true")
    assert m.member_flag("GOALS_ENABLED") is True
    from core.config_policy import AutonomyConfig
    assert AutonomyConfig.goals_enabled() is True


def test_direct_member_env_is_ignored_and_reported(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setitem(os.environ, "GIT_TOOLS_ENABLED", "false")
    assert m.member_flag("GIT_TOOLS_ENABLED") is True          # ignored
    from tools.git import git_enabled
    assert git_enabled() is True

    from core.config_policy.retired_flags import moved_into_overrides, retired_flag_lines
    assert moved_into_overrides() == [
        ("GIT_TOOLS_ENABLED", "POLYROB_OVERRIDES=GIT_TOOLS_ENABLED=false")]
    lines = retired_flag_lines(dict(os.environ))
    assert any("GIT_TOOLS_ENABLED" in ln and "POLYROB_OVERRIDES=GIT_TOOLS_ENABLED=false" in ln
               and ln.startswith("WARN") for ln in lines), lines


def test_direct_member_env_named_in_status_posture_section(monkeypatch):
    monkeypatch.setitem(os.environ, "MCP_ENABLED", "true")
    monkeypatch.setitem(os.environ, "KB_ENABLED", "0")
    monkeypatch.setitem(os.environ, m.OVERRIDES_ENV, "KB_ENABLED=true")
    from core.status_identity import _posture_section
    sec = _posture_section()
    item = next(h for h in sec.health if h.key == "posture_member_direct")
    assert "MCP_ENABLED=true" in item.text
    # the existing override entry wins in the merged line (it is what runs today)
    assert item.remedy.endswith("POLYROB_OVERRIDES=KB_ENABLED=true,MCP_ENABLED=true")


def test_unknown_and_non_member_override_names_reported(monkeypatch):
    monkeypatch.setitem(os.environ, m.OVERRIDES_ENV,
                        "NOT_A_FLAG=true,DEFAULT_MODEL=x,KB_ENABLED=maybe,garbage")
    kinds = {p.name or p.entry: p.kind for p in m.override_problems()}
    assert kinds == {"NOT_A_FLAG": "unknown", "DEFAULT_MODEL": "not_member",
                     "KB_ENABLED": "bad_value", "GARBAGE": "malformed"}
    assert m.member_flag("KB_ENABLED") is False   # typo -> bundle default
    lines = m.member_env_lines()
    assert sum("POLYROB_OVERRIDES entry ignored" in ln for ln in lines) == 4


def test_values_parse_with_the_falsey_set(monkeypatch):
    for raw, want in (("yes", True), ("on", True), ("1", True), ("no", False),
                      ("none", False), ("0", False)):
        monkeypatch.setitem(os.environ, m.OVERRIDES_ENV, f"CRON_ENABLED={raw}")
        assert m.member_flag("CRON_ENABLED") is want, raw


def test_base_on_and_composite_defaults(monkeypatch):
    assert m.member_flag("GOAL_COMPLETION_JUDGE") is True
    monkeypatch.setitem(os.environ, m.OVERRIDES_ENV, "GOAL_COMPLETION_JUDGE=false")
    assert m.member_flag("GOAL_COMPLETION_JUDGE") is False
    monkeypatch.setitem(os.environ, m.OVERRIDES_ENV, "X402_INVOICE_ENABLED=true")
    assert m.member_flag("INVOICE_CARD_ENABLED") is True


def test_compute_posture_member_follows_frozen_posture(monkeypatch):
    import importlib
    cp = importlib.import_module("core.config_policy.compute_posture")
    monkeypatch.setattr(cp, "_COMPUTE_POSTURE_FROZEN", 1)
    assert m.member_flag("SHELL_TOOLS_ENABLED") is True
    assert m.member_flag("SELF_ENV_ENABLED") is False
    monkeypatch.setitem(os.environ, m.OVERRIDES_ENV, "SHELL_TOOLS_ENABLED=false")
    assert m.member_flag("SHELL_TOOLS_ENABLED") is False


def test_non_member_name_is_refused_by_the_resolver():
    with pytest.raises(KeyError):
        m.member_flag("DEFAULT_MODEL")


def test_access_tier_reads_override_from_injected_env():
    assert m.member_flag("GROUP_CHAT_ENABLED", {m.OVERRIDES_ENV: "GROUP_CHAT_ENABLED=true"})
    assert not m.member_flag("GROUP_CHAT_ENABLED", {"GROUP_CHAT_ENABLED": "true"})


def test_writers_keep_one_line(tmp_path):
    env = {}
    m.setdefault_flag(env, "CORRESPONDENT_ACCESS_ENABLED", "true")
    m.setdefault_flag(env, "SINGULAR_CHAT_ENABLED", "true")
    assert env == {m.OVERRIDES_ENV: "CORRESPONDENT_ACCESS_ENABLED=true",
                   "SINGULAR_CHAT_ENABLED": "true"}
    env[m.OVERRIDES_ENV] = "CORRESPONDENT_ACCESS_ENABLED=false"
    m.setdefault_flag(env, "CORRESPONDENT_ACCESS_ENABLED", "true")   # operator wins
    assert env[m.OVERRIDES_ENV] == "CORRESPONDENT_ACCESS_ENABLED=false"

    f = tmp_path / ".env"
    f.write_text("EMAIL_SURFACE_ENABLED=true\nOTHER=1\n")
    assert m.upsert_flag(f, "EMAIL_SURFACE_ENABLED", "true") == m.OVERRIDES_ENV
    assert m.upsert_flag(f, "KB_ENABLED", "no") == m.OVERRIDES_ENV
    text = f.read_text()
    assert "POLYROB_OVERRIDES=EMAIL_SURFACE_ENABLED=true,KB_ENABLED=false" in text
    assert "\nEMAIL_SURFACE_ENABLED" not in "\n" + text
    assert "OTHER=1" in text


def test_config_set_member_writes_the_override_line(monkeypatch, tmp_path):
    import core.config_service as cs
    env_file = tmp_path / ".env"
    env_file.write_text("GIT_TOOLS_ENABLED=true\n")
    monkeypatch.setattr(cs, "home_env_path", lambda: env_file)
    res = cs.set_value("GIT_TOOLS_ENABLED", "false")
    assert res.ok, res.message
    assert "POLYROB_OVERRIDES" in res.message
    text = env_file.read_text()
    assert "POLYROB_OVERRIDES=GIT_TOOLS_ENABLED=false" in text
    assert "\nGIT_TOOLS_ENABLED=" not in "\n" + text
    assert not cs.set_value("GIT_TOOLS_ENABLED", "maybe").ok


def test_config_set_member_refused_from_a_remote_surface(monkeypatch, tmp_path):
    import core.config_service as cs
    monkeypatch.setattr(cs, "home_env_path", lambda: tmp_path / ".env")
    res = cs.set_value("KB_ENABLED", "true", surface="console")
    assert not res.ok and res.outcome == "refused"
    res = cs.set_value(m.OVERRIDES_ENV, "KB_ENABLED=true", surface="telegram")
    assert not res.ok and res.outcome == "refused"


def test_describe_member(monkeypatch):
    import core.config_service as cs
    monkeypatch.setitem(os.environ, m.OVERRIDES_ENV, "KB_ENABLED=true")
    info = cs.describe("KB_ENABLED", include_chain=True)
    assert info.effective is True and info.source == m.OVERRIDES_ENV
    assert "POLYROB_OVERRIDES" in info.description


def test_monkeypatch_shim_routes_members(monkeypatch):
    monkeypatch.setenv("KB_ENABLED", "1")
    monkeypatch.setenv("GOALS_ENABLED", "false")
    assert os.environ.get("KB_ENABLED") is None
    assert os.environ[m.OVERRIDES_ENV] == "KB_ENABLED=true,GOALS_ENABLED=false"
    monkeypatch.delenv("KB_ENABLED")
    assert os.environ[m.OVERRIDES_ENV] == "GOALS_ENABLED=false"
