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


def x402_invoicing_enabled() -> bool:
    """Gate for the receivables/invoicing tool (RECEIVE side only — x402_pay/wallet
    stay OFF regardless). Delegates to modules.x402.invoicing.x402_invoicing_enabled
    — the shared SSOT (013 T2 review fix, Finding 2) — so the tool-registration gate
    can never disagree with the settlement/pay-endpoint gate (api/x402_endpoints.py)
    or the autonomy-runtime settlement-watcher gate, all three of which read the same
    env var. Default OFF; ON under effective AUTONOMY_MODE=autonomous via
    _mode_capability_default. Explicit X402_INVOICE_ENABLED always wins."""
    from modules.x402.invoicing import x402_invoicing_enabled as _invoicing_enabled
    return _invoicing_enabled()


def register_x402_invoice_tool(force: bool = False) -> bool:
    """Register 'x402_invoice' (x402_request/x402_invoices/accounting) IFF
    X402_INVOICE_ENABLED (agent money loop). Same shape as register_x402_tool;
    never in default tool_ids. Distinct flag from X402_CLIENT_ENABLED — invoicing
    (receivables) needs a treasury address, not an agent wallet."""
    if not (force or x402_invoicing_enabled()):
        return False
    from tools.descriptors import (
        TOOL_DESCRIPTORS, ToolDescriptor, ToolCategory, register_tool_class,
    )
    from tools.x402.invoice_tool import X402InvoiceTool

    if "x402_invoice" not in TOOL_DESCRIPTORS:
        TOOL_DESCRIPTORS["x402_invoice"] = ToolDescriptor(
            name="x402_invoice",
            description="Create/track x402 payment requests (invoices) + the unified accounting ledger",
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=80,
        )
    register_tool_class("x402_invoice", X402InvoiceTool)
    return True
