"""Gated registration: HF_DEPLOY_ENABLED off ⇒ the tool is absent; on/force ⇒
descriptor + class registered (the register_optional_tool pattern)."""


def test_register_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("HF_DEPLOY_ENABLED", raising=False)
    from tools.hf_deploy import register_hf_deploy_tool
    assert register_hf_deploy_tool() is False


def test_flag_default_is_off(monkeypatch):
    monkeypatch.delenv("HF_DEPLOY_ENABLED", raising=False)
    from tools.hf_deploy import hf_deploy_enabled
    assert hf_deploy_enabled() is False
    # NOT in the POLYROB_LOCAL safe group: local mode must not flip it on
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert hf_deploy_enabled() is False


def test_register_when_enabled(monkeypatch):
    monkeypatch.setenv("HF_DEPLOY_ENABLED", "true")
    from tools.hf_deploy import register_hf_deploy_tool
    from tools.descriptors import TOOL_DESCRIPTORS
    assert register_hf_deploy_tool() is True
    assert "hf_deploy" in TOOL_DESCRIPTORS
    assert TOOL_DESCRIPTORS["hf_deploy"].tool_class is not None


def test_caps_defaults(monkeypatch):
    from tools.hf_deploy import hf_deploy_daily_max, hf_deploy_min_interval_sec
    monkeypatch.delenv("HF_DEPLOY_DAILY_MAX", raising=False)
    monkeypatch.delenv("HF_DEPLOY_MIN_INTERVAL_SEC", raising=False)
    assert hf_deploy_daily_max() == 10
    assert hf_deploy_min_interval_sec() == 120
    monkeypatch.setenv("HF_DEPLOY_DAILY_MAX", "3")
    monkeypatch.setenv("HF_DEPLOY_MIN_INTERVAL_SEC", "5")
    assert hf_deploy_daily_max() == 3
    assert hf_deploy_min_interval_sec() == 5


def test_never_in_default_tool_ids(monkeypatch):
    monkeypatch.setenv("HF_DEPLOY_ENABLED", "true")
    from agents.task.tool_defaults import resolve_toolset
    for name in ("default", "coding", "autonomy", "full"):
        try:
            tools = resolve_toolset(name)
        except Exception:
            continue
        assert "hf_deploy" not in (tools or []), name
