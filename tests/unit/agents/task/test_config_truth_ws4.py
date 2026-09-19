"""056 WS4 — a deploy must not REQUEST tools it has configured off.

Prod 2026-09-17→19: `AGENT_COMPUTE_POSTURE=1` made `with_compute_tools` add
`code_execution`/`shell` to every goal/cron toolset while `CODE_EXEC_ENABLED=false`
and `SHELL_TOOLS_ENABLED=false`, so 43 goal records carried a false
`[tool gap] code_execution …` line and the sandbox refused 469 times. And `email`
was requested at every session start against a Gmail app password rejected since
09-17 (330 failed loads). Posture is a CEILING; the flags still decide; a rejected
credential removes the tool from the EFFECTIVE autonomous set until it is fixed.
"""
import time

import pytest


def _reset_email_memory():
    from core import credential_verdicts as cv
    cv._reset_for_tests()


def _reject_smtp():
    from core import credential_verdicts as cv
    cv.record_rejection("smtp", "smtp.gmail.com:587")


def test_compute_tools_follow_the_flags_not_only_the_posture(monkeypatch):
    from agents.task import tool_defaults as td
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    monkeypatch.setenv("CODE_EXEC_ENABLED", "false")
    monkeypatch.setenv("SHELL_TOOLS_ENABLED", "false")
    monkeypatch.setenv("CODING_TOOLS_ENABLED", "true")
    monkeypatch.setattr("agents.task.constants.compute_posture", lambda: 1)
    tools = td.with_compute_tools(["filesystem", "task"])
    assert "coding" in tools
    assert "code_execution" not in tools and "shell" not in tools


def test_compute_tools_all_on_when_flags_on(monkeypatch):
    from agents.task import tool_defaults as td
    monkeypatch.setenv("CODE_EXEC_ENABLED", "true")
    monkeypatch.setenv("SHELL_TOOLS_ENABLED", "true")
    monkeypatch.setenv("CODING_TOOLS_ENABLED", "true")
    monkeypatch.setattr("agents.task.constants.compute_posture", lambda: 1)
    tools = td.with_compute_tools(["filesystem"])
    assert {"code_execution", "shell", "coding"} <= set(tools)


def test_posture_zero_adds_nothing(monkeypatch):
    from agents.task import tool_defaults as td
    monkeypatch.setenv("CODE_EXEC_ENABLED", "true")
    monkeypatch.setenv("SHELL_TOOLS_ENABLED", "true")
    monkeypatch.setattr("agents.task.constants.compute_posture", lambda: 0)
    assert td.with_compute_tools(["filesystem"]) == ["filesystem"]


def test_effective_autonomous_tools_drop_email_after_smtp_rejection(monkeypatch):
    from agents.task import constants as c
    _reset_email_memory()
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("AGENTMAIL_API_KEY", raising=False)
    assert "email" in c.effective_autonomous_tools(), "no rejection yet → email stays"
    _reject_smtp()
    try:
        assert "email" not in c.effective_autonomous_tools()
        # The CONSTANT is untouched (its own pin stands); only the effective set moves.
        assert "email" in c.AUTONOMOUS_MODE_TOOLS
    finally:
        _reset_email_memory()


def test_effective_autonomous_tools_keep_email_on_agentmail(monkeypatch):
    from agents.task import constants as c
    _reset_email_memory()
    monkeypatch.setenv("EMAIL_PROVIDER", "agentmail")
    _reject_smtp()
    try:
        assert "email" in c.effective_autonomous_tools(), "an SMTP rejection says nothing about AgentMail"
    finally:
        _reset_email_memory()


def test_default_goal_tools_use_the_effective_set(monkeypatch):
    from agents.task.goals import dispatcher as d
    _reset_email_memory()
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("AGENTMAIL_API_KEY", raising=False)
    monkeypatch.setattr("agents.task.constants.full_autonomy_enabled", lambda: True)
    _reject_smtp()
    try:
        assert "email" not in d.default_goal_tools()
    finally:
        _reset_email_memory()


def test_email_tool_records_the_rejection_in_the_core_register(monkeypatch):
    """The tool's 535 path feeds the register the toolset reads (no tools import
    in agents — layering ratchet)."""
    import smtplib
    from core import credential_verdicts as cv
    from tools import email_tool as et
    cv._reset_for_tests()

    class _SMTP:
        def __init__(self, *a, **k): pass
        def starttls(self, context=None): pass
        def login(self, u, p): raise smtplib.SMTPAuthenticationError(535, b"bad")
        def quit(self): pass

    monkeypatch.setattr(et.smtplib, "SMTP", _SMTP)
    tool = object.__new__(et.EmailTool)
    tool.smtp_server, tool.smtp_port = "smtp.gmail.com", 587
    tool.config = type("C", (), {"gmail_email": "a@b", "gmail_app_password": "x"})()
    tool.logger = __import__("logging").getLogger("t")
    import asyncio
    with pytest.raises(Exception):
        asyncio.run(tool._test_smtp_connection())
    assert cv.rejected_within("smtp", 900)
    assert et.smtp_credentials_rejected()
    cv._reset_for_tests()
