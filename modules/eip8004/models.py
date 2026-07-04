"""ERC-8004 Pydantic Models.

Models for the ERC-8004 Trustless Agents standard.
https://eips.ethereum.org/EIPS/eip-8004
"""

from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum


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
    """An endpoint in the registration file.
    
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
    image: Optional[str] = Field(None, description="Agent image URL")
    trustMode: str = Field(
        default="local",
        description="'onchain' only when an operator declares on-chain registration "
        "(EIP8004_ONCHAIN_ENABLED); otherwise 'local' (off-chain/simulation) so "
        "discovery does not over-promise an unverified identity.",
    )

    # Protocol endpoints
    endpoints: List[Endpoint] = Field(default_factory=list, description="List of protocol endpoints")
    
    # On-chain registrations
    registrations: List[Registration] = Field(default_factory=list, description="On-chain registrations")
    
    # Trust models
    supportedTrust: Optional[List[str]] = Field(
        None,
        description="Supported trust models: reputation, crypto-economic, tee-attestation"
    )


# =============================================================================
# Reputation Registry Models
# =============================================================================

class ProofOfPayment(BaseModel):
    """Proof of payment for x402 integration."""
    fromAddress: str = Field(..., description="Payer wallet address")
    toAddress: str = Field(..., description="Recipient wallet address")
    chainId: str = Field(..., description="Chain ID")
    txHash: str = Field(..., description="Transaction hash")


class FeedbackAuth(BaseModel):
    """Signed authorization for feedback submission.
    
    Agent signs this to authorize a client to submit feedback.
    """
    agentId: int = Field(..., description="Agent's on-chain ID")
    clientAddress: str = Field(..., description="Authorized client address")
    expiresAt: int = Field(..., description="Authorization expiry timestamp")
    nonce: str = Field(..., description="Unique nonce to prevent replay")
    signature: str = Field(..., description="Agent's EIP-712 signature")


class FeedbackEntry(BaseModel):
    """Feedback entry for the Reputation Registry."""
    
    # Required on-chain fields
    agentId: int = Field(..., description="Agent's on-chain ID")
    clientAddress: str = Field(..., description="Client wallet address")
    score: int = Field(..., ge=0, le=100, description="Score 0-100")
    tag1: Optional[str] = Field(None, max_length=32, description="Primary tag (bytes32)")
    tag2: Optional[str] = Field(None, max_length=32, description="Secondary tag (bytes32)")
    
    # Off-chain file reference
    fileUri: Optional[str] = Field(None, description="URI to off-chain feedback JSON")
    fileHash: Optional[str] = Field(None, description="KECCAK-256 hash of file content")
    
    # Authorization
    feedbackAuth: FeedbackAuth = Field(..., description="Agent's authorization signature")


class FeedbackFile(BaseModel):
    """Off-chain feedback file structure (optional).
    
    Stored at fileUri, hash verified by fileHash.
    """
    # Required
    agentRegistry: str = Field(..., description="eip155:chainId:address")
    agentId: int
    clientAddress: str
    createdAt: str = Field(default_factory=lambda: datetime.now().isoformat())
    feedbackAuth: str
    score: int
    
    # Optional categorization
    tag1: Optional[str] = None
    tag2: Optional[str] = None
    skill: Optional[str] = Field(None, description="A2A skill identifier")
    context: Optional[str] = Field(None, description="A2A context")
    task: Optional[str] = Field(None, description="A2A task identifier")
    capability: Optional[str] = Field(None, description="MCP capability: prompts, resources, tools, completions")
    name: Optional[str] = Field(None, description="MCP tool/prompt/resource name")
    
    # x402 integration
    proof_of_payment: Optional[ProofOfPayment] = None
    
    # Additional metadata
    comment: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


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


# =============================================================================
# API Request/Response Models
# =============================================================================

class CreateFeedbackAuthRequest(BaseModel):
    """Request to create a feedback authorization."""
    
    clientAddress: str = Field(..., description="Client address to authorize")
    taskId: Optional[str] = Field(None, description="Associated A2A task ID")
    expiresInSeconds: int = Field(default=86400, description="Authorization validity period")


class CreateFeedbackAuthResponse(BaseModel):
    """Response with signed feedback authorization."""
    
    feedbackAuth: FeedbackAuth
    message: str = "Feedback authorization created"


class SubmitFeedbackRequest(BaseModel):
    """Request to submit feedback."""
    
    agentId: int
    score: int = Field(..., ge=0, le=100)
    feedbackAuth: FeedbackAuth
    
    # Optional
    tag1: Optional[str] = None
    tag2: Optional[str] = None
    skill: Optional[str] = None
    taskId: Optional[str] = None
    proof_of_payment: Optional[ProofOfPayment] = None
    comment: Optional[str] = None


class GetReputationRequest(BaseModel):
    """Request to get agent reputation."""
    
    agentId: int
    clientAddress: Optional[str] = None
    tag1: Optional[str] = None
    tag2: Optional[str] = None


class ReputationSummary(BaseModel):
    """Agent reputation summary."""
    
    agentId: int
    totalFeedback: int
    averageScore: float
    recentScores: List[int]
    topTags: List[str]


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

