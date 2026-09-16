"""ERC-8004 Registration File Generator.

Generates the registration file that the Identity Registry tokenURI points to.
This file links to A2A agent card, MCP endpoints, wallet addresses, etc.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

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


#: Hosts that nobody outside this machine can resolve. An `image` or a service
#: URL on one of these is not a public endpoint, and advertising it as one is
#: the same defect as the `rob-logo.png` 404 this replaced.
_PRIVATE_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _is_public(base_url: str) -> bool:
    if not base_url:
        return False
    low = base_url.lower()
    if not low.startswith(("http://", "https://")):
        return False
    host = low.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    return host not in _PRIVATE_HOSTS and "." in host


def _agent_identity() -> tuple:
    """``(name, description)`` for THIS instance — never a framework literal.

    ⚠️ W1 neutral-identity rule: a specific bot's name is DATA (its character,
    its profile, its instance id), never code. Until 2026-09-15 this file
    advertised ``name="POLYROB"`` with a fixed marketing description, so every
    instance in the world published the framework's identity as its own.
    """
    from core.instance import resolve_instance_id
    instance_id = resolve_instance_id()
    name, description = instance_id, None
    # ⚠️ The character file is read DIRECTLY, not via
    # `agents.personality.persona_resolver`. The layering is
    # core <- modules <- agents, so this module may not import `agents.*` —
    # caught by tests/test_layering_ratchet.py, which is exactly what it is for.
    try:
        from core.runtime_paths import resolve_data_home
        for candidate in (
            Path(resolve_data_home()) / "characters" / f"{instance_id}.character.json",
            (Path(__file__).resolve().parents[2] / "data" / "characters"
             / f"{instance_id}.character.json"),
        ):
            if candidate.is_file():
                char = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(char, dict):
                    name = char.get("name") or name
                    description = char.get("description") or char.get("bio") or None
                break
    except Exception:
        logger.debug("eip8004: no character to describe this instance", exc_info=True)
    if not description:
        description = (
            f"An autonomous agent instance ({instance_id}) running on the POLYROB "
            f"framework. See the linked services for what it can actually do."
        )
    return name, description


def _avatar(base_url: str) -> tuple:
    """``(image_url_or_None, avatar_metadata_or_None)`` for the frozen Mindprint.

    ⚠️ Returns ``None`` for the image rather than a guess. With no public base
    URL there is nowhere to serve the PNG from, and a link that does not resolve
    is worse than an absent field. The seed goes out instead, so the face stays
    exactly reproducible.
    """
    try:
        from core.instance import load_pfp_meta, pfp_path, resolve_instance_id
        from core.runtime_paths import resolve_data_home
        home, instance_id = resolve_data_home(), resolve_instance_id()
        if not pfp_path(home, instance_id).is_file():
            return None, None
        meta = load_pfp_meta(home, instance_id) or {}
        avatar = {k: meta[k] for k in ("generator", "seed", "variant", "seed_hex")
                  if meta.get(k)}
        image = f"{base_url.rstrip('/')}/pfp.png" if _is_public(base_url) else None
        return image, (avatar or None)
    except Exception:
        logger.debug("eip8004: could not resolve the instance avatar", exc_info=True)
        return None, None


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
    # Proposal 013 (T2): the same OR as core/config.py's consumer seam — under
    # effective AUTONOMY_MODE=autonomous, MCP defaults ON here too so the agent
    # card never disagrees with what core/config.py actually built. Lazy +
    # guarded import (fail to False) to avoid an import cycle / hard dependency.
    try:
        from core.config_policy import _mode_capability_default
        mcp_enabled = mcp_enabled or _mode_capability_default("MCP_ENABLED")
    except Exception:
        pass
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
    
    # Agent Wallet (for payments/signing) — W2.2 (2026-08-21): fall back to the
    # SAME resolver invoices/the agent card use (env wins, wallet fills in),
    # so every advertised address agrees.
    from modules.x402.x402_integration import resolve_treasury_address
    agent_wallet = config.agent_wallet or resolve_treasury_address()
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
    # 046: a VERIFIED record beats an operator claim. `register_agent` writes
    # this file only from a confirmed on-chain receipt, so it is evidence rather
    # than configuration — and the two are never both emitted, because two
    # registrations[] entries would read as two identities.
    verified = None
    try:
        from core.instance import load_erc8004_record, resolve_instance_id
        from core.runtime_paths import resolve_data_home
        verified = load_erc8004_record(resolve_data_home(), resolve_instance_id())
    except Exception:
        logger.debug("eip8004: could not read the on-chain record", exc_info=True)

    onchain_enabled = os.environ.get("EIP8004_ONCHAIN_ENABLED", "false").lower() == "true"
    trust_mode = "onchain" if (verified or onchain_enabled) else "local"

    registrations = []
    if verified:
        registrations.append(Registration(
            agentId=int(verified["agent_id"]),
            agentRegistry=(f"eip155:{verified.get('chain_id', config.chain_id)}:"
                           f"{verified['registry']}"),
            # ⚠️ "verified", not "operator": this one is backed by a transaction
            # this code signed, broadcast and confirmed.
            attestation="verified",
        ))

    if (not verified) and onchain_enabled and config.agent_id and config.identity_registry_address:
        # L11: EIP8004_ONCHAIN_ENABLED + agent_id/identity_registry_address are
        # operator-supplied env config, not proof of an on-chain transaction — no
        # code in this repo ever signs/broadcasts an Identity Registry registration.
        # Mark the claim honestly so a consumer can't read this as code-verified.
        registrations.append(Registration(
            agentId=config.agent_id,
            agentRegistry=f"eip155:{config.chain_id}:{config.identity_registry_address}",
            attestation="operator",
        ))

    # Build the registration file
    name, description = _agent_identity()
    image, avatar = _avatar(base_url)
    registration_file = RegistrationFile(
        name=name,
        description=description,
        image=image,
        trustMode=trust_mode,
        services=endpoints,
        x402Support=x402_enabled,
        active=True,
        metadata=({"avatar": avatar} if avatar else None),
        registrations=registrations,
        supportedTrust=config.supported_trust if config.enabled else None
    )
    
    logger.info(f"Built ERC-8004 registration file with {len(endpoints)} endpoints")
    return registration_file

