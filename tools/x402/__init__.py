"""Agent x402 paying tool package (gated; default OFF)."""
import os


def x402_client_enabled() -> bool:
    return os.getenv("X402_CLIENT_ENABLED", "false").lower() == "true"


def register_x402_tool(force: bool = False) -> bool:
    """Register the 'x402_pay' descriptor + class IFF X402_CLIENT_ENABLED (or forced).

    Mirrors tools/code_exec.register_code_exec_tool. Descriptor inserted first
    (register_tool_class is a silent no-op for unknown names). Never in default tool_ids.
    """
    if not (force or x402_client_enabled()):
        return False
    from tools.descriptors import (
        TOOL_DESCRIPTORS, ToolDescriptor, ToolCategory, register_tool_class,
    )
    from tools.x402.service import X402PayTool

    if "x402_pay" not in TOOL_DESCRIPTORS:
        TOOL_DESCRIPTORS["x402_pay"] = ToolDescriptor(
            name="x402_pay",
            description="Pay for paid resources via the x402 protocol (agent personal wallet)",
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=80,
        )
    register_tool_class("x402_pay", X402PayTool)
    return True
