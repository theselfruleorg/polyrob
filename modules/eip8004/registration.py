"""ERC-8004 Registration File Generator.

Generates the registration file that the Identity Registry tokenURI points to.
This file links to A2A agent card, MCP endpoints, wallet addresses, etc.
"""

import os
import logging
from typing import Optional, Dict, Any

from .models import RegistrationFile, Endpoint, Registration, EIP8004Config

logger = logging.getLogger(__name__)


def get_eip8004_config() -> EIP8004Config:
    """Get ERC-8004 configuration from environment."""
    return EIP8004Config(
        enabled=os.environ.get("EIP8004_ENABLED", "false").lower() == "true",
        chain_id=int(os.environ.get("EIP8004_CHAIN_ID", "8453")),  # Base
        identity_registry_address=os.environ.get("EIP8004_IDENTITY_REGISTRY"),
        reputation_registry_address=os.environ.get("EIP8004_REPUTATION_REGISTRY"),
        validation_registry_address=os.environ.get("EIP8004_VALIDATION_REGISTRY"),
        agent_id=int(os.environ.get("EIP8004_AGENT_ID", "0")) or None,
        agent_wallet=os.environ.get("EIP8004_AGENT_WALLET"),
        supported_trust=os.environ.get("EIP8004_SUPPORTED_TRUST", "reputation").split(","),
        ipfs_gateway=os.environ.get("IPFS_GATEWAY", "https://ipfs.io/ipfs/"),
    )


def build_registration_file(
    base_url: Optional[str] = None,
    config: Optional[EIP8004Config] = None,
) -> RegistrationFile:
    """Build the ERC-8004 registration file.
    
    This file is what the Identity Registry tokenURI resolves to.
    It contains all endpoints for discovering and interacting with the agent.
    
    Args:
        base_url: Base URL of the agent service
        config: Optional EIP8004Config, loaded from env if not provided
        
    Returns:
        RegistrationFile with all agent endpoints and registrations
    """
    if config is None:
        config = get_eip8004_config()
    
    base_url = base_url or os.environ.get("A2A_BASE_URL", "http://localhost:9000")
    
    # Build endpoints list
    endpoints = []
    
    # A2A Endpoint (primary)
    endpoints.append(Endpoint(
        name="A2A",
        endpoint=f"{base_url}/.well-known/agent.json",
        version="1.0"
    ))
    
    # MCP Endpoint (if enabled)
    # SA-08: use the core.env SSOT parser so MCP_ENABLED means the SAME thing here as in
    # core/config.py (pydantic bool). The old `== "true"` treated MCP_ENABLED=1 as False
    # while BotConfig treated it as True — the agent card could advertise MCP as disabled
    # while MCP was actually running.
    from core.env import bool_env
    mcp_enabled = bool_env("MCP_ENABLED", False)
    if mcp_enabled:
        endpoints.append(Endpoint(
            name="MCP",
            endpoint=f"{base_url}/mcp",
            version="2025-06-18",
            capabilities={
                "tools": True,
                "resources": True,
                "prompts": True,
            }
        ))
    
    # Agent Wallet (for payments/signing)
    agent_wallet = config.agent_wallet or os.environ.get("X402_PAYMENT_RECIPIENT")
    if agent_wallet:
        # Format: eip155:chainId:address
        endpoints.append(Endpoint(
            name="agentWallet",
            endpoint=f"eip155:{config.chain_id}:{agent_wallet}"
        ))
    
    # x402 Payment Endpoint
    x402_enabled = os.environ.get("X402_ENABLED", "false").lower() == "true"
    if x402_enabled:
        endpoints.append(Endpoint(
            name="x402",
            endpoint=f"{base_url}/api/x402/pricing",
            version="1.0"
        ))
    
    # ERC-8004 specific endpoints
    if config.enabled:
        endpoints.append(Endpoint(
            name="EIP8004-reputation",
            endpoint=f"{base_url}/eip8004/reputation",
            version="1.0"
        ))
        endpoints.append(Endpoint(
            name="EIP8004-validation",
            endpoint=f"{base_url}/eip8004/validation",
            version="1.0"
        ))
    
    # Trust mode: only CLAIM an on-chain identity when an operator explicitly
    # declares it (EIP8004_ONCHAIN_ENABLED). Until the on-chain write path exists
    # and ownership is verified, advertise honest "local" (off-chain) mode and do
    # NOT emit a registrations[] block we cannot back.
    onchain_enabled = os.environ.get("EIP8004_ONCHAIN_ENABLED", "false").lower() == "true"
    trust_mode = "onchain" if onchain_enabled else "local"

    registrations = []
    if onchain_enabled and config.agent_id and config.identity_registry_address:
        registrations.append(Registration(
            agentId=config.agent_id,
            agentRegistry=f"eip155:{config.chain_id}:{config.identity_registry_address}"
        ))

    # Build the registration file
    registration_file = RegistrationFile(
        name="POLYROB",
        description=(
            "AI automation agent with browser control, file system access, "
            "MCP integrations, and autonomous task execution capabilities. "
            "Supports x402 pay-per-request payments and A2A protocol for agent interoperability."
        ),
        image=f"{base_url}/static/images/rob-logo.png",
        trustMode=trust_mode,
        endpoints=endpoints,
        registrations=registrations,
        supportedTrust=config.supported_trust if config.enabled else None
    )
    
    logger.info(f"Built ERC-8004 registration file with {len(endpoints)} endpoints")
    return registration_file


def get_registration_file_dict(base_url: Optional[str] = None) -> Dict[str, Any]:
    """Get registration file as dictionary for JSON serialization."""
    return build_registration_file(base_url).model_dump(exclude_none=True)

