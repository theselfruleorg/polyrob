"""098 CL-05: the default model moves only after a successful live swap, through
one owner of the three stores; reset clears all three; failures are said."""
import json

import pytest

from tests.unit.cli.ui.test_commands import _FakeSwapAgent, _plain_ctx  # noqa: F401
from cli.ui.commands import default_registry


@pytest.fixture
def homes(tmp_path, monkeypatch):
    ph = tmp_path / "ph"
    ph.mkdir()
    monkeypatch.setenv("POLYROB_HOME", str(ph))
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(ph / "cli.json"))
    import core.paths as cp
    monkeypatch.setattr(cp, "polyrob_home", lambda: ph)
    import core.runtime_paths as rp
    monkeypatch.setattr(rp, "prefs_home_dir", lambda: str(tmp_path / "prefs"))
    import agents.task.model_switch_service as svc

    async def plan(cmd, *, user_id, current=(None, None)):
        return svc.Plan(cmd.provider or "anthropic", cmd.name, "id", cmd.reasoning,
                        cmd.default), ""
    monkeypatch.setattr(svc, "plan_switch", plan)
    monkeypatch.setattr("cli.ui.commands.h_model._tenant", lambda ctx: "u_owner")
    return ph


def _ctx(result):
    agent = _FakeSwapAgent(result)
    convo = type("Conv", (), {})()
    convo.agent = agent
    ctx, buf = _plain_ctx(conversation=convo)
    return ctx, buf


def _stores(ph):
    from cli.config_store import get_default_model
    from core import model_pins
    env = (ph / ".env").read_text() if (ph / ".env").exists() else ""
    return get_default_model(), "DEFAULT_MODEL" in env, model_pins.owner_default("u_owner")


@pytest.mark.asyncio
async def test_failed_swap_does_not_move_default(homes):
    ctx, buf = _ctx({"ok": False, "error": "no key", "previous": {}})
    await default_registry().dispatch("/model anthropic claude-x default", ctx)
    assert _stores(homes) == ((None, None), False, None)
    assert "did not change" in buf.getvalue()


@pytest.mark.asyncio
async def test_reset_default_clears_all_three(homes):
    ctx, buf = _ctx({"ok": True, "provider": "anthropic", "model": "claude-x", "previous": {}})
    await default_registry().dispatch("/model anthropic claude-x default", ctx)
    (prov_model, env_has, pin) = _stores(homes)
    assert prov_model == ("anthropic", "claude-x") and env_has and pin
    await default_registry().dispatch("/model reset default", ctx)
    assert _stores(homes) == ((None, None), False, None)
    assert "cleared" in buf.getvalue()


@pytest.mark.asyncio
async def test_env_write_failure_is_reported(homes, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr("core.env_file.upsert_env_var", boom)
    ctx, buf = _ctx({"ok": True, "provider": "anthropic", "model": "claude-x", "previous": {}})
    await default_registry().dispatch("/model anthropic claude-x default", ctx)
    out = buf.getvalue()
    assert ".env ✗" in out and "read-only" in out and "cli.json ✓" in out


@pytest.mark.asyncio
async def test_reasoning_default_write_failure_is_reported(homes, monkeypatch):
    from core import model_pins
    monkeypatch.setattr(model_pins, "set_default_reasoning",
                        lambda uid, r, home_dir=None: (False, "disk full"))
    monkeypatch.setattr("agents.task.model_pin_state.set_session_reasoning",
                        lambda sid, r: None)
    ctx, buf = _ctx({"ok": True, "provider": "anthropic", "model": "claude-x", "previous": {}})
    await default_registry().dispatch("/model reasoning=high default", ctx)
    out = buf.getvalue()
    assert "Reasoning high" in out and "NOT saved" in out and "disk full" in out
