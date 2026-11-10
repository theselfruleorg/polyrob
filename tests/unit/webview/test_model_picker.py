"""113 W6 item 5 (G-4): the model for new chats on Agent › Settings.

The write runs ``model_switch_service.model_command`` with the words of
``/model <name> default`` (the one ``/model`` pipeline every seat calls), with no
chat named; it is a console write (read-only and CSRF guard, owner console only,
audited as ``console_model_set``); the read never shows the operator default as
the owner's own choice when the store would not read.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    import webview.pages as pages
    import webview.model_routes as mod
    import webview.audit as audit
    import webview.webgate as webgate
    import agents.task.model_switch_service as mss
    import webview.server as server
    calls, audits = [], []

    async def fake_command(task_agent, *, user_id, words, chat_key=None, session_id=None, seat="telegram"):
        calls.append({"user_id": user_id, "words": list(words), "chat_key": chat_key,
                      "session_id": session_id, "seat": seat})
        return "✅ Model: m1 (p1)" if words[0] != "nope" else "❌ No such model nope. Nothing changed."

    monkeypatch.setattr(mss, "model_command", fake_command)
    monkeypatch.setattr(server, "_in_process_task_agent", lambda: object())
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "alice")
    monkeypatch.setattr(mod, "console_write", lambda kind, **kw: audits.append((kind, kw)))
    monkeypatch.setattr(webgate, "is_owner_console", lambda: True)
    monkeypatch.setattr(webgate, "read_only", lambda: False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(mod.router)
    return type("R", (), {"client": TestClient(app), "calls": calls, "audits": audits,
                          "mod": mod, "webgate": webgate})


def test_a_name_runs_model_name_default_with_no_chat(rig):
    r = rig.client.post("/api/webgate/model", json={"name": "deepseek-pro"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert rig.calls == [{"user_id": "alice", "words": ["deepseek-pro", "default"],
                          "chat_key": None, "session_id": None, "seat": "console"}]
    assert rig.audits[0][0] == "console_model_set"
    assert rig.audits[0][1]["user_id"] == "alice" and rig.audits[0][1]["attrs"]["ok"] is True


def test_reset_runs_model_reset_default(rig):
    rig.client.post("/api/webgate/model", json={"reset": True})
    assert rig.calls[-1]["words"] == ["reset", "default"]


def test_a_refusal_is_shown_verbatim(rig):
    r = rig.client.post("/api/webgate/model", json={"name": "nope"})
    assert r.status_code == 409 and r.json()["message"].startswith("❌ No such model")


@pytest.mark.parametrize("name", ["", "a b c", "default", "go", "x reasoning=high", "reset",
                                  "bad name!", "x" * 200])
def test_a_line_that_is_not_one_model_is_refused_before_the_pipeline(rig, name):
    r = rig.client.post("/api/webgate/model", json={"name": name})
    assert r.status_code == 400 and rig.calls == []


def test_a_tenant_console_may_not_set_it(rig, monkeypatch):
    monkeypatch.setattr(rig.webgate, "is_owner_console", lambda: False)
    assert rig.client.post("/api/webgate/model", json={"name": "m1"}).status_code == 403
    assert rig.calls == []


def test_read_only_refuses_the_write(rig, monkeypatch):
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    monkeypatch.setattr(rig.webgate, "read_only", lambda: True)
    assert rig.client.post("/api/webgate/model", json={"name": "m1"}).status_code == 403
    assert rig.calls == []


def test_the_read_says_whose_choice_it_is(rig, monkeypatch):
    from core import model_pins
    monkeypatch.setattr(model_pins, "chat_runtime_for",
                        lambda uid: ("p1", "m1", None, "your default (/model <name> default)"))
    monkeypatch.setattr(model_pins, "load", lambda uid: ({"default": {}}, ""))
    monkeypatch.setattr(model_pins, "chat_pin_for", lambda uid: {"provider": "p1", "model": "m1"})
    body = rig.client.get("/api/webgate/model").json()
    assert body["readable"] is True and body["model"] == "m1" and body["mine"] is True
    assert body["can_set"] is True


def test_an_unreadable_choice_is_named(rig, monkeypatch):
    from core import model_pins
    monkeypatch.setattr(model_pins, "chat_runtime_for",
                        lambda uid: ("p0", "m0", None, "DEFAULT_MODEL (operator env) — ⚠️ …"))
    monkeypatch.setattr(model_pins, "load", lambda uid: ({}, "TOMLDecodeError: bad"))
    monkeypatch.setattr(model_pins, "chat_pin_for", lambda uid: None)
    body = rig.client.get("/api/webgate/model").json()
    assert body["readable"] is False and "TOMLDecodeError" in body["error"]


def test_the_write_carries_the_console_guards():
    import webview.model_routes as mod
    from webview import webgate
    post = [r for r in mod.router.routes if "POST" in r.methods][0]
    deps = {d.dependency.__name__ for d in post.dependencies}   # a reload elsewhere makes new objects
    assert {g.dependency.__name__ for g in webgate.MUTATION_DEPS} <= deps
