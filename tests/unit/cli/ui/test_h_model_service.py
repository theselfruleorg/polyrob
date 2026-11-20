"""The REPL /model routes reasoning / reset / alias / aliases through the ONE
service the chat seats call (agents/task/model_switch_service.model_command); the
REPL keeps only the glue that applies a queued switch at once."""
import asyncio
from types import SimpleNamespace

import pytest

from cli.ui.commands import h_model


class _Agent:
    def __init__(self, provider="deepseek", model="deepseek-chat"):
        self.llm_provider, self.model_name = provider, model
        self.session_id = "s1"
        self._pending = None
        self.applied = []

    def request_model_pin(self, pin):
        self._pending = dict(pin)

    def pending_model_pin(self):
        return self._pending

    def cancel_model_pin(self):
        self._pending = None

    async def apply_pending_model_pin(self):
        pin, self._pending = self._pending, None
        self.applied.append(pin)
        self.llm_provider, self.model_name = pin["provider"], pin["model"]
        res = {"ok": True, "provider": pin["provider"], "model": pin["model"]}
        if callable(pin.get("on_applied")):
            await pin["on_applied"](res)
        return res

    async def swap_model(self, *a, **k):  # pragma: no cover - the set path only
        return {"ok": True}


def _ctx(agent, args):
    out = []
    ctx = SimpleNamespace(args=args, user_id="local", agent=agent, task_agent=None,
                          session_id="s1", state=SimpleNamespace(model=None, provider=None),
                          emit=lambda text, **k: out.append(text))
    return ctx, out


@pytest.fixture(autouse=True)
def _tenant(monkeypatch):
    monkeypatch.setattr(h_model, "_tenant", lambda ctx: "owner-1")


async def _pick(_cur):  # pragma: no cover - the bare /model only
    return None


def test_token_level_on_deepseek_is_refused_like_the_chat_seats(monkeypatch):
    seen = []
    monkeypatch.setattr("agents.task.model_pin_state.set_session_reasoning",
                        lambda sid, lvl: seen.append(lvl))
    ctx, out = _ctx(_Agent(), ["reasoning=2000"])
    asyncio.run(h_model.handle(ctx, pick_fn=_pick))
    assert "not a token count" in out[-1]
    assert seen == []


def test_reasoning_names_the_providers_that_send_it(monkeypatch):
    seen = []
    monkeypatch.setattr("agents.task.model_pin_state.set_session_reasoning",
                        lambda sid, lvl: seen.append((sid, lvl)))
    ctx, out = _ctx(_Agent(), ["reasoning=high"])
    asyncio.run(h_model.handle(ctx, pick_fn=_pick))
    assert seen == [("s1", "high")]
    assert "(sent on OpenRouter)" not in out[-1]
    assert "Reasoning high" in out[-1]


def test_reset_resolves_through_the_chat_runtime_and_swaps_now(monkeypatch):
    monkeypatch.setattr("core.model_pins.chat_runtime_for",
                        lambda uid: ("openrouter", "vendor/m1", None, "your default"))
    import cli.config_store as cs
    monkeypatch.setattr(cs, "get_default_model",
                        lambda: pytest.fail("the REPL must not read cli.json for reset"),
                        raising=False)
    agent = _Agent()
    ctx, out = _ctx(agent, ["reset"])
    asyncio.run(h_model.handle(ctx, pick_fn=_pick))
    assert agent.applied and agent.applied[0]["model"] == "vendor/m1"
    assert ctx.state.model == "vendor/m1" and ctx.state.provider == "openrouter"
    assert "Back to vendor/m1" in out[0]


def test_aliases_route_through_the_service(monkeypatch):
    calls = []

    async def fake(task_agent, **kw):
        calls.append(kw)
        return "ALIASES"
    monkeypatch.setattr("agents.task.model_switch_service.model_command", fake)
    ctx, out = _ctx(_Agent(), ["aliases"])
    asyncio.run(h_model.handle(ctx, pick_fn=_pick))
    assert out == ["ALIASES"]
    assert calls[0]["seat"] == "repl" and calls[0]["words"] == ["aliases"]
