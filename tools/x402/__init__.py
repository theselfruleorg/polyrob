"""Agent x402 paying tool package (gated; default OFF)."""
import os


def x402_client_enabled() -> bool:
    return os.getenv("X402_CLIENT_ENABLED", "false").lower() == "true"


def register_x402_tool(force: bool = False) -> bool:
    """Register the 'x402_pay' descriptor + class IFF X402_CLIENT_ENABLED (or forced).

    Delegates to ``register_optional_tool`` (single shared factory, F1 2026-09-14) so
    the capability-classification guard in ``tools/descriptors.py`` actually fires for
    this tool — it was previously inserted via a direct ``TOOL_DESCRIPTORS``/
    ``register_tool_class`` call that bypassed ``is_classified()``. Descriptor is
    byte-identical; never in default tool_ids.
    """
    from tools.descriptors import ToolDescriptor, ToolCategory, register_optional_tool
    from tools.x402.service import X402PayTool

    return register_optional_tool(
        "x402_pay",
        X402PayTool,
        ToolDescriptor(
            name="x402_pay",
            description=("Discover and pay x402 resources: probe/sweep endpoints read-only "
                         "for price + payability (no wallet needed), and pay via the agent wallet"),
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=80,
        ),
        x402_client_enabled,
        force=force,
    )


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
    X402_INVOICE_ENABLED (agent money loop). Same shape as register_x402_tool
    — routes through ``register_optional_tool`` (F1 2026-09-14) so the
    capability-classification guard fires. Never in default tool_ids. Distinct
    flag from X402_CLIENT_ENABLED — invoicing (receivables) needs a treasury
    address, not an agent wallet."""
    from tools.descriptors import ToolDescriptor, ToolCategory, register_optional_tool
    from tools.x402.invoice_tool import X402InvoiceTool

    return register_optional_tool(
        "x402_invoice",
        X402InvoiceTool,
        ToolDescriptor(
            name="x402_invoice",
            description="Create/track x402 payment requests (invoices) + the unified accounting ledger",
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=80,
        ),
        x402_invoicing_enabled,
        force=force,
    )
