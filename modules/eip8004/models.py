"""ERC-8004 Pydantic Models.

Models for the ERC-8004 Trustless Agents standard.
https://eips.ethereum.org/EIPS/eip-8004
"""

from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field
from enum import Enum

# M21 (2026-07-15 wallet/crypto review): ValidationManager is a
# process-volatile in-memory simulation — a restart wipes it — and
# its reads stay served even when EIP8004_ENABLED is off. Rather than hard-gate
# the reads, every response carries this honest marker.
_SIMULATION_NOTE = (
    "This registry is a local, process-volatile simulation (in-memory; a restart "
    "wipes it) — not read from an on-chain registry. See docs/guide/payments.md "
    "§ERC-8004."
)


# =============================================================================
# Configuration
# =============================================================================

class EIP8004Config(BaseModel):
    """Configuration for ERC-8004 integration."""
    
    # Identity Registry
    enabled: bool = Field(default=False, description="Enable ERC-8004 integration")
    chain_id: int = Field(default=8453, description="Chain ID (default: Base)")
    identity_registry_address: Optional[str] = Field(None, description="Identity Registry contract address")
    reputation_registry_address: Optional[str] = Field(None, description="Reputation Registry contract address")
    validation_registry_address: Optional[str] = Field(None, description="Validation Registry contract address")
    
    # Agent Identity
    agent_id: Optional[int] = Field(None, description="On-chain agent ID (ERC-721 tokenId)")
    agent_wallet: Optional[str] = Field(None, description="Agent's wallet address for signing")
    
    # Trust Models
    supported_trust: List[str] = Field(
        default=["reputation"],
        description="Supported trust models: reputation, crypto-economic, tee-attestation"
    )
    
    # IPFS/Storage
    ipfs_gateway: str = Field(default="https://ipfs.io/ipfs/", description="IPFS gateway URL")
    registration_file_storage: Literal["ipfs", "https"] = Field(
        default="https",
        description="Where to host the registration file"
    )


# =============================================================================
# Registration File Models (Identity Registry)
# =============================================================================

class Endpoint(BaseModel):
    """One entry in the registration file's ``services`` array.

    The class keeps its name (every existing importer uses it) while the FIELD
    it populates was renamed ``endpoints`` -> ``services`` by the current
    ERC-8004 draft. Renaming the list must not silently change each entry's
    shape, which is what `test_a_service_entry_keeps_the_endpoint_shape` pins.

    Supports various protocols: A2A, MCP, ENS, DID, agentWallet, etc.
    """
    name: str = Field(..., description="Endpoint type (A2A, MCP, ENS, DID, agentWallet)")
    endpoint: str = Field(..., description="Endpoint URL/identifier")
    version: Optional[str] = Field(None, description="Protocol version")
    capabilities: Optional[Dict[str, Any]] = Field(None, description="Optional capabilities (for MCP)")


class Registration(BaseModel):
    """Agent registration on a specific chain."""
    agentId: int = Field(..., description="ERC-721 tokenId")
    agentRegistry: str = Field(..., description="Registry identifier (eip155:chainId:address)")
    attestation: Optional[str] = Field(
        None,
        description="L11 (2026-07-15 wallet/crypto review): 'operator' marks that this "
        "registration is the operator's own out-of-band claim (e.g. a manual on-chain "
        "mint the operator performed themselves) — no code in this repository has ever "
        "signed or broadcast the on-chain registration transaction. Never omit this when "
        "the entry is emitted so a consumer can't mistake it for a code-verified fact.",
    )


class RegistrationFile(BaseModel):
    """ERC-8004 Registration File.
    
    The tokenURI of the ERC-721 resolves to this file.
    """
    type: str = Field(
        default="https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        description="Schema type identifier"
    )
    name: str = Field(..., description="Agent name")
    description: str = Field(..., description="Agent description")
    #: The agent's face. ⚠️ OMITTED (None) rather than guessed when there is
    #: nowhere public to serve it from — this field was hardcoded to a
    #: `<name>-logo.png` that does not exist anywhere in the tree, so every file
    #: ever served pointed at a 404. A broken link is worse than no link.
    image: Optional[str] = Field(None, description="Agent image URL")
    trustMode: str = Field(
        default="local",
        description="'onchain' only when an operator declares on-chain registration "
        "(EIP8004_ONCHAIN_ENABLED); otherwise 'local' (off-chain/simulation) so "
        "discovery does not over-promise an unverified identity.",
    )

    # Protocol endpoints. ⚠️ Named `services` to match the CURRENT draft; the
    # file emitted `endpoints` until 2026-09-15, so a consumer reading the spec
    # found none of the fields it expected.
    services: List[Endpoint] = Field(default_factory=list,
                                     description="List of protocol endpoints")

    #: Whether this agent accepts x402 payments. Derived from whether the rail
    #: is actually armed — advertising a payment method that is switched off is
    #: the same class of lie as advertising an endpoint that 404s.
    x402Support: bool = Field(default=False, description="Accepts x402 payments")

    #: Whether this agent is currently serving.
    active: bool = Field(default=True, description="Agent is active")

    #: Free-form, spec-permitted. Carries `avatar` = {source, sha256} of the
    #: instance's avatar image (`core/avatar.py`), so a consumer can check that
    #: the image it fetched is the one the agent declared — a URL can be
    #: swapped; a hash cannot.
    metadata: Optional[Dict[str, Any]] = Field(
        None, description="Additional agent metadata")
    
    # On-chain registrations
    registrations: List[Registration] = Field(default_factory=list, description="On-chain registrations")
    
    # Trust models
    supportedTrust: Optional[List[str]] = Field(
        None,
        description="Supported trust models: reputation, crypto-economic, tee-attestation"
    )


# =============================================================================
# Validation Registry Models
# =============================================================================

class ValidationRequestModel(BaseModel):
    """Request for validation from the Validation Registry."""
    
    validatorAddress: str = Field(..., description="Validator contract address")
    agentId: int = Field(..., description="Agent's on-chain ID")
    requestUri: str = Field(..., description="URI to validation request data")
    requestHash: str = Field(..., description="KECCAK-256 hash of request data (optional if IPFS)")


class ValidationResponseModel(BaseModel):
    """Response from a validator."""
    
    requestHash: str = Field(..., description="Hash of original request")
    response: int = Field(..., ge=0, le=100, description="Validation result 0-100")
    responseUri: Optional[str] = Field(None, description="URI to validation evidence")
    responseHash: Optional[str] = Field(None, description="Hash of response data")
    tag: Optional[str] = Field(None, description="Custom tag (bytes32)")


class ValidationStatus(BaseModel):
    """Validation status from on-chain query."""

    validatorAddress: str
    agentId: int
    response: int
    tag: Optional[str]
    lastUpdate: int  # Unix timestamp
    simulated: bool = Field(default=True, description=_SIMULATION_NOTE)


# =============================================================================
# API Request/Response Models
# =============================================================================

class RequestValidationRequest(BaseModel):
    """Request to request validation."""
    
    validatorAddress: str
    requestData: Dict[str, Any] = Field(..., description="Data for validator")
    

class ValidationSummary(BaseModel):
    """Validation summary for an agent."""

    agentId: int
    totalValidations: int
    averageResponse: float
    validatorBreakdown: Dict[str, int]
    simulated: bool = Field(default=True, description=_SIMULATION_NOTE)

