"""CHAT-1: in local mode code runs on the operator's host. A paired user, an
allowlisted second chat or any other network principal that reached a session
must not run code there; the owner tenant still may."""
from types import SimpleNamespace

import tools.code_exec.sandbox_guard as g


def test_local_mode_host_exec_is_owner_only(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    assert "owner tenant" in g.local_host_exec_refusal(SimpleNamespace(user_id="u_paired"))
    assert "owner tenant" in g.local_host_exec_refusal(SimpleNamespace(user_id=None))
    assert "owner tenant" in g.local_host_exec_refusal(None)
    assert g.local_host_exec_refusal(SimpleNamespace(user_id="u_owner")) is None


def test_unbound_local_install_keeps_the_local_operator(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    assert g.local_host_exec_refusal(SimpleNamespace(user_id="local")) is None
    assert g.local_host_exec_refusal(SimpleNamespace(user_id="u_slack_x"))


def test_server_mode_is_unchanged(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "false")
    assert g.local_host_exec_refusal(SimpleNamespace(user_id="u_x")) is None
