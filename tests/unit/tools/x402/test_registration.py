"""Task 10 — gated registration of the x402_pay tool."""


def test_register_x402_tool_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("X402_CLIENT_ENABLED", raising=False)
    from tools.x402 import register_x402_tool
    assert register_x402_tool() is False


def test_register_x402_tool_registers_when_forced():
    from tools.x402 import register_x402_tool
    from tools.descriptors import TOOL_DESCRIPTORS
    assert register_x402_tool(force=True) is True
    assert "x402_pay" in TOOL_DESCRIPTORS
    assert TOOL_DESCRIPTORS["x402_pay"].tool_class is not None


def test_x402_registrars_route_through_register_optional_tool(monkeypatch):
    """F1 (2026-09-14): both x402_pay and x402_invoice previously built a
    ToolDescriptor and called register_tool_class DIRECTLY, bypassing the
    is_classified() guard in register_optional_tool (tools/descriptors.py).
    Both tools ARE classified in core/tool_capabilities.py, but nothing
    enforced it — a future edit to either registrar could silently drop the
    guard again. Assert both registrars now call the shared factory.
    """
    import tools.descriptors as descriptors_module
    from tools.descriptors import TOOL_DESCRIPTORS

    had_pay = "x402_pay" in TOOL_DESCRIPTORS
    prior_pay = TOOL_DESCRIPTORS.get("x402_pay")
    had_invoice = "x402_invoice" in TOOL_DESCRIPTORS
    prior_invoice = TOOL_DESCRIPTORS.get("x402_invoice")

    calls = []
    real_register_optional_tool = descriptors_module.register_optional_tool

    def _spy(name, tool_cls, descriptor, enabled_fn, *, force=False):
        calls.append(name)
        return real_register_optional_tool(name, tool_cls, descriptor, enabled_fn, force=force)

    monkeypatch.setattr(descriptors_module, "register_optional_tool", _spy)
    try:
        from tools.x402 import register_x402_tool, register_x402_invoice_tool
        assert register_x402_tool(force=True) is True
        assert register_x402_invoice_tool(force=True) is True
        assert calls == ["x402_pay", "x402_invoice"]
    finally:
        if had_pay:
            TOOL_DESCRIPTORS["x402_pay"] = prior_pay
        else:
            TOOL_DESCRIPTORS.pop("x402_pay", None)
        if had_invoice:
            TOOL_DESCRIPTORS["x402_invoice"] = prior_invoice
        else:
            TOOL_DESCRIPTORS.pop("x402_invoice", None)


def test_register_optional_tool_refuses_an_unclassified_x402_shaped_stand_in():
    """The guard F1 wires x402_pay/x402_invoice through: an unclassified tool
    (even one shaped like a money tool) refuses registration rather than
    silently skipping every money/high-impact/delegate-block gate. Mirrors
    tests/unit/core/test_tool_capabilities.py::
    test_registration_guard_refuses_unclassified_tool with an x402-flavored id
    so this file's own coverage doesn't rely solely on that other module.
    """
    import pytest
    from tools.base_tool import BaseTool
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool
    from core.tool_capabilities import is_classified

    class _UnclassifiedPayStandIn(BaseTool):  # pragma: no cover - never initialized
        pass

    desc = ToolDescriptor(
        name="phantom_x402_pay_stand_in",
        description="test-only",
        category=ToolCategory.INTEGRATION,
    )
    with pytest.raises(ValueError, match="capabilit"):
        register_optional_tool(
            "phantom_x402_pay_stand_in", _UnclassifiedPayStandIn, desc,
            lambda: False, force=True,
        )
    assert not is_classified("phantom_x402_pay_stand_in")
