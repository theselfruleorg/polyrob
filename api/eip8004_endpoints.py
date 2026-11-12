"""ERC-8004 Trustless Agents API Endpoints.

Implements the HTTP API for ERC-8004 integration:
- Registration file endpoint (for Identity Registry tokenURI)
- Validation endpoints (request, response, status)

There is no feedback endpoint: feedback lives on the on-chain Reputation
Registry, and the agent reads it there (``core/wallet/erc8004_reputation.py``).

Reference: https://eips.ethereum.org/EIPS/eip-8004
"""

import os
from core.env import bool_env
import logging
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, Field

from modules.eip8004 import (
    build_registration_file,
    ValidationManager,
)
from modules.eip8004.registration import get_eip8004_config
from modules.eip8004.models import ValidationSummary

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/eip8004", tags=["EIP-8004 Trustless Agents"])

# Singleton manager
_validation_manager: Optional[ValidationManager] = None


def require_eip8004_enabled() -> bool:
    """Gate write endpoints behind EIP8004_ENABLED (404 when off).

    Discovery/read endpoints stay open; only the state-changing surface is hidden
    unless an operator explicitly enables ERC-8004.
    """
    if not bool_env("EIP8004_ENABLED", False):
        raise HTTPException(status_code=404, detail="ERC-8004 not enabled")
    return True


def require_owner_or_admin(request: Request) -> bool:
    """Require admin/owner for the ERC-8004 validation-queue writes.

    B20: decide from ``role`` via the ONE admin predicate
    (``core.constants.is_admin_role``, re-exported by ``api.auth_constants``),
    not from ``request.state.is_admin`` — that flag is a SECOND truth, written
    by only some of the auth paths, so an owner-login session (role="owner")
    authenticated through a path that never set it was refused, and any future
    path that sets it without a role would be admitted.
    """
    from api.auth_constants import is_admin_role

    if not is_admin_role(getattr(request.state, "role", None)):
        raise HTTPException(status_code=403, detail="Admin access required")
    return True


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
    # The registry advertises the operator-configured origin, never a caller's Host.
    registration = build_registration_file()
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
# Validation Registry Endpoints
# =============================================================================

class ValidationRequestBody(BaseModel):
    """Request body for validation request."""
    validatorAddress: str = Field(..., description="Validator contract address")
    requestData: Dict[str, Any] = Field(..., description="Data to validate")


@router.post(
    "/validation/request",
    dependencies=[Depends(require_eip8004_enabled), Depends(require_owner_or_admin)],
)
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


@router.get("/validation/pending", response_model=List[Dict[str, Any]])
async def list_pending_validations(
    agent_id: Optional[int] = None,
    manager: ValidationManager = Depends(get_validation_manager),
):
    """List pending validation requests.

    Args:
        agent_id: Optional filter by agent ID

    Returns:
        Pending validation requests (bare list, original shape). Each item is
        stamped `simulated: true` (M21: served even when EIP8004_ENABLED is
        off — the validation registry is a local, process-volatile
        simulation, not an on-chain registry).
    """
    pending = await manager.list_pending_validations(agent_id=agent_id)
    return [{**item, "simulated": True} for item in pending]


@router.get("/validation/validators", response_model=Dict[str, str])
async def list_validators(
    manager: ValidationManager = Depends(get_validation_manager),
):
    """List supported validator types.

    Returns:
        Validator types and descriptions (bare dict, original shape), each
        description suffixed " (simulated)" (M21: this is a static local
        list, not read from an on-chain registry).
    """
    return {
        name: f"{description} (simulated)"
        for name, description in manager.get_supported_validators().items()
    }
