"""095 W2: seat-aware status, [loaded] tools that refuse every call, one text home."""
from types import SimpleNamespace

from core.security.policy_order import install_session_gates
from core.surfaces.room_policy import make_room_gate_hook
from tools.tool_disclosure import render_tool_catalog, resolve_tool_status, seat_probe


class _Ctl:
    def __init__(self):
        self.hooks = []

    def register_pre_tool_call_hook(self, hook, fail_mode="closed"):
        self.hooks.append(hook)


def _room_controller(public=True):
    ctl = _Ctl()
    install_session_gates(ctl, correspondent_hook=lambda *a: None,
                          room_hook=make_room_gate_hook(lambda: public))
    return ctl


def test_the_room_seat_probe_is_stored_and_read():
    ctl = _room_controller()
    probe = seat_probe(ctl)
    assert probe("shell") == "room" and probe("web_fetch") is None
    st = resolve_tool_status("coding", container=None, loaded_ids=set(), seat_denies=probe)
    assert (st.status, st.reason) == ("gated", "seat-room")
    assert "load_tool" not in st.remedy and "operator" not in st.remedy


def test_a_private_session_has_no_seat_gate():
    probe = seat_probe(_room_controller(public=False))
    assert probe("shell") is None


def test_the_room_catalog_offers_no_load_tool_and_no_money_note():
    text = render_tool_catalog(container=None, loaded_ids={"web_fetch"},
                               seat_denies=seat_probe(_room_controller()))
    assert "Money note" not in text and "propose_action" not in text
    assert "capture-session" not in text and "polyrob doctor" not in text


def test_the_correspondent_seat_keeps_the_reply_tool():
    from agents.task.agent.core.correspondent_gate import build_correspondent_gate
    orch = SimpleNamespace(_correspondent_session=True, container=None, user_id="u")
    hook = build_correspondent_gate(None, orch)
    assert hook.seat_denies("email") is None
    assert hook.seat_denies("shell") == "correspondent"
    orch._correspondent_session = False
    assert hook.seat_denies("shell") is None


def test_a_tool_that_did_not_start_is_tool_off():
    from tools.base_tool import BaseTool
    tool = BaseTool.__new__(BaseTool)
    tool._enabled = False
    reason, remedy = tool.availability_gap()
    assert reason == "tool-off" and remedy.startswith("operator ")


def test_a_wallet_tool_without_a_wallet_is_wallet_off(monkeypatch):
    from tools.wallet_holder import WalletHolderMixin
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    assert WalletHolderMixin()._wallet_gap()[0] == "wallet-off"
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    assert WalletHolderMixin()._wallet_gap() is None


def test_no_remedy_prints_a_replacing_overrides_line():
    from core.tool_gates import REMEDIES, remedy_text
    for key in REMEDIES:
        assert "POLYROB_OVERRIDES=" not in remedy_text(key, tool="t", flag="SOME_FLAG"), key
