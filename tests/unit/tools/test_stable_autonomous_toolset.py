"""057 WS-A: the requested autonomous toolset stops flipping on a credential TTL.

Dropping and re-adding `email` on a 900 s verdict TTL changed the emitted tool
schemas, which are part of the cached prompt prefix — four distinct tool counts
in one prod day, each a cold cache. Under STABLE_AUTONOMOUS_TOOLSET the set is
frozen and the rejection is told in the catalog instead.
"""
import pytest

from agents.task.constants import effective_autonomous_tools


@pytest.fixture
def smtp_rejected(monkeypatch):
    import core.credential_verdicts as cv
    monkeypatch.setattr(cv, "rejected_within", lambda kind, secs: kind == "smtp")
    import core.config_policy.capability_toggles as ct
    monkeypatch.setattr(ct, "email_provider", lambda: "smtp")
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")


def test_legacy_default_still_drops_email(monkeypatch, smtp_rejected):
    monkeypatch.delenv("STABLE_AUTONOMOUS_TOOLSET", raising=False)
    import agents.task.constants as c
    monkeypatch.setattr(c, "autonomous_mode_tools", lambda: ("filesystem", "email", "task"))
    assert "email" not in effective_autonomous_tools()


def test_stable_toolset_keeps_the_bytes_frozen(monkeypatch, smtp_rejected):
    monkeypatch.setenv("STABLE_AUTONOMOUS_TOOLSET", "true")
    import agents.task.constants as c
    monkeypatch.setattr(c, "autonomous_mode_tools", lambda: ("filesystem", "email", "task"))
    assert effective_autonomous_tools() == ("filesystem", "email", "task")


def test_the_catalog_says_credentials_rejected_instead(monkeypatch, smtp_rejected):
    from tools.tool_disclosure import resolve_tool_status
    st = resolve_tool_status("email", container=None, loaded_ids={"email"})
    assert st.status == "gated" and st.reason == "credentials-rejected"
    assert "535" in st.remedy or "SMTP" in st.remedy
    assert st.remedy, "a gated tool must always name its remedy"


def test_an_agentmail_deploy_is_not_gated_by_an_smtp_verdict(monkeypatch, smtp_rejected):
    import core.config_policy.capability_toggles as ct
    monkeypatch.setattr(ct, "email_provider", lambda: "agentmail")
    from tools.tool_disclosure import resolve_tool_status
    st = resolve_tool_status("email", container=None, loaded_ids={"email"})
    assert st.status == "loaded"


def test_no_verdict_means_loaded(monkeypatch):
    import core.credential_verdicts as cv
    monkeypatch.setattr(cv, "rejected_within", lambda kind, secs: False)
    from tools.tool_disclosure import resolve_tool_status
    st = resolve_tool_status("email", container=None, loaded_ids={"email"})
    assert st.status == "loaded"


def test_an_unreadable_verdict_store_fails_open(monkeypatch):
    import core.credential_verdicts as cv

    def _boom(kind, secs):
        raise RuntimeError("verdicts.db is gone")

    monkeypatch.setattr(cv, "rejected_within", _boom)
    from tools.tool_disclosure import resolve_tool_status
    st = resolve_tool_status("email", container=None, loaded_ids={"email"})
    assert st.status == "loaded"
