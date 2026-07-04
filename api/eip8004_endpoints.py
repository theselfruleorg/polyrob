"""ERC-8004 Trustless Agents API Endpoints.

Implements the HTTP API for ERC-8004 integration:
- Registration file endpoint (for Identity Registry tokenURI)
- Reputation endpoints (feedback auth, submission, querying)
- Validation endpoints (request, response, status)

Reference: https://eips.ethereum.org/EIPS/eip-8004
"""

import os
import logging
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, Field

from modules.eip8004 import (
    build_registration_file,
    ReputationManager,
    ValidationManager,
    EIP8004Config,
)
from modules.eip8004.registration import get_eip8004_config
from modules.eip8004.models import (
    CreateFeedbackAuthRequest,
    CreateFeedbackAuthResponse,
    SubmitFeedbackRequest,
    GetReputationRequest,
    ReputationSummary,
    RequestValidationRequest,
    ValidationSummary,
    ProofOfPayment,
    FeedbackAuth,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/eip8004", tags=["EIP-8004 Trustless Agents"])

# Singleton managers
_reputation_manager: Optional[ReputationManager] = None
_validation_manager: Optional[ValidationManager] = None


def require_eip8004_enabled() -> bool:
    """Gate write endpoints behind EIP8004_ENABLED (404 when off).

    Discovery/read endpoints stay open; only the state-changing surface is hidden
    unless an operator explicitly enables ERC-8004.
    """
    if os.environ.get("EIP8004_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=404, detail="ERC-8004 not enabled")
    return True


def require_owner_or_admin(request: Request) -> bool:
    """Require admin/owner for sensitive ERC-8004 writes.

    /reputation/authorize signs an EIP-712 payload with the AGENT'S private key,
    so it must never be callable anonymously (it would be a signing oracle).
    """
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")
    return True


def get_reputation_manager() -> ReputationManager:
    """Get or create reputation manager singleton."""
    global _reputation_manager
    if _reputation_manager is None:
        _reputation_manager = ReputationManager()
    return _reputation_manager


def get_validation_manager() -> ValidationManager:
    """Get or create validation manager singleton."""
    global _validation_manager
    if _validation_manager is None:
        _validation_manager = ValidationManager()
    return _validation_manager


# =============================================================================
# Registration / Discovery Endpoints
# =============================================================================

@router.get("/registration.json", response_model=Dict[str, Any])
async def get_registration_file(request: Request):
    """Get the ERC-8004 registration file.
    
    This is what the Identity Registry tokenURI points to.
    Contains all agent endpoints (A2A, MCP, wallets) and trust models.
    
    Returns:
        Registration file JSON per ERC-8004 spec
    """
    # Determine base URL
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    base_url = f"{scheme}://{host}"
    
    registration = build_registration_file(base_url)
    return registration.model_dump(exclude_none=True)


@router.get("/config")
async def get_config():
    """Get ERC-8004 configuration status.
    
    Returns:
        Current configuration (addresses, chain, supported trust models)
    """
    config = get_eip8004_config()
    return {
        "enabled": config.enabled,
        "chainId": config.chain_id,
        "agentId": config.agent_id,
        "identityRegistry": config.identity_registry_address,
        "reputationRegistry": config.reputation_registry_address,
        "validationRegistry": config.validation_registry_address,
        "supportedTrust": config.supported_trust,
    }


# =============================================================================
# Reputation Registry Endpoints
# =============================================================================

class FeedbackAuthRequest(BaseModel):
    """Request body for creating feedback authorization."""
    clientAddress: str = Field(..., description="Client wallet address to authorize")
    taskId: Optional[str] = Field(None, description="Optional A2A task ID")
    expiresInSeconds: int = Field(default=86400, description="Validity period (default 24h)")


class FeedbackAuthResponse(BaseModel):
    """Response with feedback authorization."""
    agentId: int
    clientAddress: str
    expiresAt: int
    nonce: str
    signature: str
    message: str = "Feedback authorization created"


@router.post(
    "/reputation/authorize",
    response_model=FeedbackAuthResponse,
    dependencies=[Depends(require_eip8004_enabled), Depends(require_owner_or_admin)],
)
async def create_feedback_authorization(
    body: FeedbackAuthRequest,
    manager: ReputationManager = Depends(get_reputation_manager),
):
    """Create a signed authorization for a client to submit feedback.
    
    This endpoint is called by the agent after accepting a task.
    The client uses the returned authorization when submitting feedback.
    
    Args:
        body: Client address and optional task ID
        
    Returns:
        Signed FeedbackAuth per ERC-8004 spec
    """
    try:
        auth = await manager.create_feedback_auth(
            client_address=body.clientAddress,
            task_id=body.taskId,
            expires_in_seconds=body.expiresInSeconds,
        )
        
        return FeedbackAuthResponse(
            agentId=auth.agentId,
            clientAddress=auth.clientAddress,
            expiresAt=auth.expiresAt,
            nonce=auth.nonce,
            signature=auth.signature,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create feedback auth: {e}")
        raise HTTPException(status_code=500, detail="Failed to create authorization")


class SubmitFeedbackBody(BaseModel):
    """Request body for feedback submission."""
    agentId: int = Field(..., description="Agent's on-chain ID")
    score: int = Field(..., ge=0, le=100, description="Score 0-100")
    
    # Authorization (from /reputation/authorize)
    feedbackAuth: Dict[str, Any] = Field(..., description="Authorization from agent")
    
    # Optional fields
    tag1: Optional[str] = Field(None, description="Primary categorization tag")
    tag2: Optional[str] = Field(None, description="Secondary tag")
    skill: Optional[str] = Field(None, description="A2A skill identifier")
    taskId: Optional[str] = Field(None, description="A2A task ID")
    comment: Optional[str] = Field(None, description="Text feedback")
    
    # x402 integration
    proofOfPayment: Optional[Dict[str, str]] = Field(None, description="x402 payment proof")


@router.post("/reputation/feedback", dependencies=[Depends(require_eip8004_enabled)])
async def submit_feedback(
    body: SubmitFeedbackBody,
    manager: ReputationManager = Depends(get_reputation_manager),
):
    """Submit feedback for an agent.
    
    Called by clients after completing a task. Requires valid authorization
    from the agent (obtained via /reputation/authorize).
    
    Args:
        body: Feedback data including score, auth, and optional tags
        
    Returns:
        Submission result with file hash
    """
    try:
        # Parse feedback auth
        auth = FeedbackAuth(**body.feedbackAuth)
        
        # Parse proof of payment if provided
        pop = None
        if body.proofOfPayment:
            pop = ProofOfPayment(**body.proofOfPayment)
        
        result = await manager.submit_feedback(
            agent_id=body.agentId,
            score=body.score,
            feedback_auth=auth,
            tag1=body.tag1,
            tag2=body.tag2,
            skill=body.skill,
            task_id=body.taskId,
            proof_of_payment=pop,
            comment=body.comment,
        )
        
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to submit feedback: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit feedback")


class ReputationQuery(BaseModel):
    """Query parameters for reputation lookup."""
    agentId: int
    clientAddress: Optional[str] = None
    tag1: Optional[str] = None
    tag2: Optional[str] = None


@router.post("/reputation/query", response_model=ReputationSummary)
async def query_reputation(
    body: ReputationQuery,
    manager: ReputationManager = Depends(get_reputation_manager),
):
    """Query reputation for an agent.
    
    Returns aggregated feedback statistics with optional filtering.
    
    Args:
        body: Query filters (agent ID, client, tags)
        
    Returns:
        ReputationSummary with scores and stats
    """
    return await manager.get_reputation(
        agent_id=body.agentId,
        client_address=body.clientAddress,
        tag1=body.tag1,
        tag2=body.tag2,
    )


@router.get("/reputation/{agent_id}")
async def get_reputation(
    agent_id: int,
    limit: int = 50,
    offset: int = 0,
    manager: ReputationManager = Depends(get_reputation_manager),
):
    """Get reputation summary and feedback list for an agent.
    
    Args:
        agent_id: Agent's on-chain ID
        limit: Max feedback entries to return
        offset: Pagination offset
        
    Returns:
        Summary and list of feedback entries
    """
    summary = await manager.get_reputation(agent_id=agent_id)
    feedback_list = await manager.get_feedback_list(
        agent_id=agent_id,
        limit=limit,
        offset=offset,
    )
    
    return {
        "summary": summary.model_dump(),
        "feedback": feedback_list,
    }


# =============================================================================
# Validation Registry Endpoints
# =============================================================================

class ValidationRequestBody(BaseModel):
    """Request body for validation request."""
    validatorAddress: str = Field(..., description="Validator contract address")
    requestData: Dict[str, Any] = Field(..., description="Data to validate")


@router.post("/validation/request", dependencies=[Depends(require_eip8004_enabled)])
async def request_validation(
    body: ValidationRequestBody,
    manager: ValidationManager = Depends(get_validation_manager),
):
    """Request validation from a validator.
    
    Submits data to be validated by a specific validator contract.
    
    Args:
        body: Validator address and request data
        
    Returns:
        Request result with hash for tracking
    """
    try:
        result = await manager.request_validation(
            validator_address=body.validatorAddress,
            request_data=body.requestData,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to request validation: {e}")
        raise HTTPException(status_code=500, detail="Failed to request validation")


class ValidationResponseBody(BaseModel):
    """Request body for validation response."""
    requestHash: str = Field(..., description="Hash of original request")
    response: int = Field(..., ge=0, le=100, description="Result 0-100")
    responseData: Optional[Dict[str, Any]] = Field(None, description="Evidence data")
    tag: Optional[str] = Field(None, description="Categorization tag")


@router.post(
    "/validation/respond",
    dependencies=[Depends(require_eip8004_enabled), Depends(require_owner_or_admin)],
)
async def submit_validation_response(
    body: ValidationResponseBody,
    manager: ValidationManager = Depends(get_validation_manager),
):
    """Submit a validation response.
    
    Called by validators after processing a request.
    
    Args:
        body: Request hash, response value, optional evidence
        
    Returns:
        Response submission result
    """
    try:
        result = await manager.submit_response(
            request_hash=body.requestHash,
            response=body.response,
            response_data=body.responseData,
            tag=body.tag,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to submit validation response: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit response")


@router.get("/validation/status/{request_hash}")
async def get_validation_status(
    request_hash: str,
    manager: ValidationManager = Depends(get_validation_manager),
):
    """Get status of a validation request.
    
    Args:
        request_hash: Hash of the validation request
        
    Returns:
        Validation status with response and timestamp
    """
    status = await manager.get_validation_status(request_hash)
    if status is None:
        raise HTTPException(status_code=404, detail="Validation request not found")
    
    return status.model_dump()


@router.get("/validation/summary/{agent_id}", response_model=ValidationSummary)
async def get_validation_summary(
    agent_id: int,
    manager: ValidationManager = Depends(get_validation_manager),
):
    """Get validation summary for an agent.
    
    Args:
        agent_id: Agent's on-chain ID
        
    Returns:
        ValidationSummary with aggregated stats
    """
    return await manager.get_validation_summary(agent_id=agent_id)


@router.get("/validation/pending")
async def list_pending_validations(
    agent_id: Optional[int] = None,
    manager: ValidationManager = Depends(get_validation_manager),
):
    """List pending validation requests.
    
    Args:
        agent_id: Optional filter by agent ID
        
    Returns:
        List of pending validation requests
    """
    return await manager.list_pending_validations(agent_id=agent_id)


@router.get("/validation/validators")
async def list_validators(
    manager: ValidationManager = Depends(get_validation_manager),
):
    """List supported validator types.
    
    Returns:
        Dictionary of validator types and descriptions
    """
    return manager.get_supported_validators()

