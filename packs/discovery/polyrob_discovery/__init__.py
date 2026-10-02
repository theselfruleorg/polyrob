"""The discovery pack (067 P3a): AnySite structured web data + Perplexity search.

The code half of the pack. Importing this module is cheap (core only); the tool
modules are named by reference and imported in the loader's phase 2. The policy
rows (capabilities, per-action policy, catalog permissions) are in
``pack.toml`` and register in phase 1, before any pack code runs.
"""
from pathlib import Path

from core.packs.spec import PackSpec, ToolContribution


def anysite_gate() -> bool:
    """The live anysite gate (``ANYSITE_TOOL_ENABLED``, default on). Reads the
    module attribute at call time, so a patch of
    ``polyrob_discovery.anysite.anysite_cli_enabled`` takes effect."""
    from polyrob_discovery import anysite
    return anysite.anysite_cli_enabled()


def pack() -> PackSpec:
    return PackSpec(
        id="discovery",
        tools=(
            ToolContribution(id="anysite",
                             registrar="polyrob_discovery.anysite.tool:register_anysite_tool",
                             gate="polyrob_discovery:anysite_gate"),
            ToolContribution(id="perplexity",
                             registrar="polyrob_discovery.perplexity:register_perplexity_tool"),
        ),
        skills_dir=Path(__file__).parent / "skills",
    )
