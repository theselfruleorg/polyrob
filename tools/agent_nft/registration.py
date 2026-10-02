"""Registration + gate for the optional ``agent_nft`` tool (050 §7.6; 069 names)."""
from __future__ import annotations


def agent_nft_enabled() -> bool:
    """Whether the agent_nft tool is registered. Default OFF."""
    from core.env import bool_env
    return bool_env("AGENT_NFT_ENABLED", False)


def register_agent_nft_tool(force: bool = False) -> bool:
    """Register the ``agent_nft`` descriptor + class IFF enabled (or forced)."""
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool
    from tools.agent_nft.tool import AgentNftTool

    return register_optional_tool(
        "agent_nft",
        AgentNftTool,
        ToolDescriptor(
            name="agent_nft",
            description=("Act through an agent NFT this instance owns: an ERC-721 whose ERC-6551 "
                         "token-bound account the owner controls. Verbs: agent_nft_snapshot / "
                         "agent_nft_inspect / agent_nft_journal / agent_nft_bind_identity / "
                         "agent_nft_revoke_all / agent_nft_collection_mint / "
                         "agent_nft_withdraw_token / agent_nft_collection_reveal."),
            category=ToolCategory.INTEGRATION,
            required_config=[],
            init_priority=47,
            is_optional=True,
        ),
        agent_nft_enabled,
        force=force,
    )
