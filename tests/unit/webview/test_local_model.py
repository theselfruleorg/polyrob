"""A console on one person's computer can answer; a server console holds no model."""
import asyncio

import pytest

from webview import local_model, webgate


@pytest.fixture(autouse=True)
def _restore_console_mark(monkeypatch):
    monkeypatch.setattr("tools.controller.approval._CONSOLE_PROCESS", False)


@pytest.fixture
def calls(monkeypatch):
    seen = {"tools": 0, "llm": True}

    async def fake_llm(_container, _config, *, require):
        assert require is False
        return seen["llm"]

    async def fake_tools(_container):
        seen["tools"] += 1

    monkeypatch.setattr("polyrob.app.bootstrap.register_llm_service", fake_llm)
    monkeypatch.setattr("polyrob.app.bootstrap.register_cli_tools", fake_tools)
    return seen


def test_local_console_gets_the_model_and_tools(monkeypatch, calls):
    monkeypatch.setattr(webgate, "is_local", lambda: True)
    assert asyncio.run(local_model.boot_local_model(object(), object())) is True
    assert calls["tools"] == 1


def test_a_local_console_with_no_key_still_starts(monkeypatch, calls):
    monkeypatch.setattr(webgate, "is_local", lambda: True)
    calls["llm"] = False
    assert asyncio.run(local_model.boot_local_model(object(), object())) is False
    assert calls["tools"] == 0


def test_a_server_console_builds_no_model(monkeypatch):
    monkeypatch.setattr(webgate, "is_local", lambda: False)

    async def refuse(*_a, **_k):
        pytest.fail("a server console holds no provider key")

    monkeypatch.setattr("polyrob.app.bootstrap.register_llm_service", refuse)
    assert asyncio.run(local_model.boot_local_model(object(), object())) is False


def test_no_key_message_is_the_local_one(monkeypatch):
    from webview.copy import t
    assert "polyrob auth add" in t("chat.open.no_key")
    assert "polyrob auth add" not in t("chat.open.no_model")
