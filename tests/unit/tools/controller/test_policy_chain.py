"""W5 (D3): the policy hook chain is built in ONE place, in ONE order."""
import asyncio
import pathlib
import types

import pytest

from core.security.policy_order import ORDER, SEAT_STAGES

REPO = pathlib.Path(__file__).resolve().parents[4]

# stage name -> (module, factory) for the Controller-tier stages that are
# always registered (approval stages depend on configuration).
_FACTORIES = {
    "wallet_authority": ("tools.controller.wallet_authority", "make_wallet_authority_hook"),
    "effect_gate": ("tools.controller.effect_hooks", "make_effect_gate_hook"),
    "effect_record": ("tools.controller.effect_hooks", "make_effect_record_hook"),
    "command_guard": ("tools.controller.command_guard_hook", "make_command_guard_hook"),
}


def _tagged(name):
    def factory(*a, **k):
        def hook(*args, **kw):
            return None
        hook.stage = name
        return hook
    return factory


def test_controller_registers_the_stages_in_order(monkeypatch, tmp_path):
    import importlib
    for name, (mod, attr) in _FACTORIES.items():
        monkeypatch.setattr(importlib.import_module(mod), attr, _tagged(name))
    monkeypatch.setenv("POLYROB_TOOL_DENYLIST", "never_this")
    import tools.controller.policy_chain as chain
    monkeypatch.setattr(chain, "make_denylist_hook", _tagged("denylist"))
    import agents.task.agent.service  # noqa: F401
    from tools.controller.service import Controller
    orch = types.SimpleNamespace(session_id="s1", user_id="u1", workspace_dir=str(tmp_path),
                                 _correspondent_tainted=False)
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    c = Controller(container=container, orchestrator=orch)
    pre = [getattr(e[0], "stage", None) for e in c._hooks.pre]
    post = [getattr(e[0], "stage", None) for e in c._hooks.post]
    pre = [s for s in pre if s]
    post = [s for s in post if s]
    want_pre = [n for n, tier, kind, _ in ORDER
                if tier == "controller" and kind == "pre" and (n in _FACTORIES or n == "denylist")]
    want_post = [n for n, tier, kind, _ in ORDER if tier == "controller" and kind == "post"]
    assert pre == want_pre
    assert post == want_post


def test_session_gates_follow_the_controller_tier():
    from core.security.policy_order import install_session_gates
    seen = []

    class C:
        def register_pre_tool_call_hook(self, hook, fail_mode="open"):
            seen.append((hook, fail_mode))
    install_session_gates(C(), correspondent_hook="corr", room_hook="room")
    assert seen == [("corr", "closed"), ("room", "closed")]
    names = [n for n, *_ in ORDER]
    assert names.index("correspondent_gate") > names.index("payment_approval")


def test_construction_installs_through_the_one_order():
    text = (REPO / "agents/task/agent/core/construction.py").read_text()
    assert "install_session_gates(" in text
    assert "register_pre_tool_call_hook(" not in text


def test_seat_stages_are_chain_stages():
    names = {n for n, *_ in ORDER}
    assert set(SEAT_STAGES) <= names
    assert not any(s.startswith("approval") or s == "payment_approval" for s in SEAT_STAGES)


# -- the seat chain ------------------------------------------------------------

def _seat_ctx():
    from core.security.turn_facts import owner_seat_context
    return owner_seat_context("owner-1", seat="api")


def test_seat_call_runs_the_denylist(monkeypatch):
    from tools.controller.policy_chain import SeatRefused, run_seat_action
    monkeypatch.setenv("POLYROB_TOOL_DENYLIST", "hyperliquid_place_market_order")
    called = []

    async def call():
        called.append(1)
        return "ok"
    with pytest.raises(SeatRefused, match="denylist"):
        asyncio.run(run_seat_action(tool_id="hyperliquid", action="hyperliquid_place_market_order",
                                    params={}, execution_context=_seat_ctx(), call=call))
    assert called == []


def test_seat_call_runs_and_records(monkeypatch):
    from tools.controller import effect_hooks
    from tools.controller.policy_chain import run_seat_action
    monkeypatch.delenv("POLYROB_TOOL_DENYLIST", raising=False)
    recorded = []
    monkeypatch.setattr(effect_hooks, "_record",
                        lambda *a: recorded.append(a[1]))

    async def call():
        return types.SimpleNamespace(error=None)
    out = asyncio.run(run_seat_action(
        tool_id="hyperliquid", action="hyperliquid_place_market_order", params={},
        execution_context=_seat_ctx(), call=call))
    assert out.error is None
    assert recorded == ["hyperliquid_place_market_order"]


def test_seat_owner_turn_is_not_paused(monkeypatch):
    """The effect gate never gates the owner at a seat (the owner is in the loop)."""
    from tools.controller import effect_hooks
    ctx = _seat_ctx()
    assert effect_hooks._autonomous(ctx, None) is False
