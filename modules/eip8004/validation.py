"""ERC-8004 Validation Registry Manager.

Handles validation requests and responses for the Validation Registry.
Supports stake-secured, zkML, and TEE validation models.
"""

import os
import logging
import hashlib
import json
import time
from typing import Optional, List, Dict, Any

from .models import (
    ValidationRequestModel,
    ValidationResponseModel,
    ValidationStatus,
    ValidationSummary,
)
from .registration import get_eip8004_config

logger = logging.getLogger(__name__)


class ValidationManager:
    """Manages validation requests/responses for ERC-8004 Validation Registry."""
    
    # Known validator types and their addresses
    VALIDATOR_TYPES = {
        "stake-secured": "Validation via stake-secured inference re-execution",
        "zkml": "Validation via zkML cryptographic proofs",
        "tee": "Validation via TEE (Trusted Execution Environment) attestation",
        "judge": "Validation via trusted third-party judges",
    }
    
    def __init__(self):
        """Initialize the validation manager."""
        self.config = get_eip8004_config()
        self._requests: Dict[str, ValidationRequestModel] = {}
        self._responses: Dict[str, List[ValidationResponseModel]] = {}
        self._validator_stats: Dict[str, Dict[str, int]] = {}
    
    async def request_validation(
        self,
        validator_address: str,
        request_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Request validation from a validator.
        
        Args:
            validator_address: Address of the validator contract
            request_data: Data to be validated (inputs, outputs, etc.)
            
        Returns:
            Request result with hash for tracking
        """
        if not self.config.agent_id:
            raise ValueError("Agent ID not configured (EIP8004_AGENT_ID)")
        
        # Serialize request data
        request_json = json.dumps(request_data, sort_keys=True)
        request_hash = "0x" + hashlib.sha256(request_json.encode()).hexdigest()
        
        # Create request model
        request = ValidationRequestModel(
            validatorAddress=validator_address,
            agentId=self.config.agent_id,
            requestUri=f"data:application/json,{request_json}",  # Inline data URI
            requestHash=request_hash,
        )
        
        # Store request
        self._requests[request_hash] = request
        
        logger.info(f"Validation requested: {request_hash[:16]}... to validator {validator_address}")
        
        # In production, this would:
        # 1. Upload request data to IPFS
        # 2. Call ValidationRegistry.validationRequest() on-chain
        
        return {
            "success": True,
            "requestHash": request_hash,
            "validatorAddress": validator_address,
            "agentId": self.config.agent_id,
            "message": "Validation request submitted (local mode)",
        }
    
    async def submit_response(
        self,
        request_hash: str,
        response: int,
        response_uri: Optional[str] = None,
        response_data: Optional[Dict[str, Any]] = None,
        tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit a validation response.
        
        This is called by validators after processing a request.
        
        Args:
            request_hash: Hash of the original request
            response: Validation result 0-100 (0=failed, 100=passed)
            response_uri: Optional URI to evidence
            response_data: Optional response data (serialized to responseUri)
            tag: Optional tag for categorization
            
        Returns:
            Response submission result
        """
        # Check request exists
        if request_hash not in self._requests:
            raise ValueError(f"Unknown request hash: {request_hash}")
        
        # Validate response
        if not 0 <= response <= 100:
            raise ValueError("Response must be 0-100")
        
        # Build response URI from data if provided
        if response_data and not response_uri:
            response_json = json.dumps(response_data, sort_keys=True)
            response_uri = f"data:application/json,{response_json}"
        
        response_hash = None
        if response_uri:
            response_hash = "0x" + hashlib.sha256(response_uri.encode()).hexdigest()
        
        # Create response model
        validation_response = ValidationResponseModel(
            requestHash=request_hash,
            response=response,
            responseUri=response_uri,
            responseHash=response_hash,
            tag=tag,
        )
        
        # Store response
        if request_hash not in self._responses:
            self._responses[request_hash] = []
        self._responses[request_hash].append(validation_response)
        
        # Update validator stats
        request = self._requests[request_hash]
        validator = request.validatorAddress
        if validator not in self._validator_stats:
            self._validator_stats[validator] = {"count": 0, "total_response": 0}
        self._validator_stats[validator]["count"] += 1
        self._validator_stats[validator]["total_response"] += response
        
        logger.info(f"Validation response submitted: {request_hash[:16]}... = {response}")
        
        return {
            "success": True,
            "requestHash": request_hash,
            "response": response,
            "message": "Validation response recorded (local mode)",
        }
    
    async def get_validation_status(
        self,
        request_hash: str,
    ) -> Optional[ValidationStatus]:
        """Get the status of a validation request.
        
        Args:
            request_hash: Hash of the request
            
        Returns:
            ValidationStatus or None if not found
        """
        if request_hash not in self._requests:
            return None
        
        request = self._requests[request_hash]
        responses = self._responses.get(request_hash, [])
        
        if not responses:
            return None
        
        # Return latest response
        latest = responses[-1]
        return ValidationStatus(
            validatorAddress=request.validatorAddress,
            agentId=request.agentId,
            response=latest.response,
            tag=latest.tag,
            lastUpdate=int(time.time()),
        )
    
    async def get_validation_summary(
        self,
        agent_id: int,
        validator_addresses: Optional[List[str]] = None,
        tag: Optional[str] = None,
    ) -> ValidationSummary:
        """Get validation summary for an agent.
        
        Args:
            agent_id: Agent's on-chain ID
            validator_addresses: Filter by validators
            tag: Filter by tag
            
        Returns:
            ValidationSummary with aggregated stats
        """
        # Filter requests for this agent
        agent_requests = {
            h: r for h, r in self._requests.items()
            if r.agentId == agent_id
        }
        
        if validator_addresses:
            agent_requests = {
                h: r for h, r in agent_requests.items()
                if r.validatorAddress in validator_addresses
            }
        
        # Aggregate responses
        total = 0
        response_sum = 0
        validator_counts: Dict[str, int] = {}
        
        for request_hash, request in agent_requests.items():
            responses = self._responses.get(request_hash, [])
            
            for resp in responses:
                if tag and resp.tag != tag:
                    continue
                    
                total += 1
                response_sum += resp.response
                
                validator = request.validatorAddress
                validator_counts[validator] = validator_counts.get(validator, 0) + 1
        
        avg_response = response_sum / total if total > 0 else 0.0
        
        return ValidationSummary(
            agentId=agent_id,
            totalValidations=total,
            averageResponse=round(avg_response, 2),
            validatorBreakdown=validator_counts,
        )
    
    async def list_pending_validations(
        self,
        agent_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """List pending validation requests.
        
        Args:
            agent_id: Filter by agent ID
            
        Returns:
            List of pending requests
        """
        pending = []
        
        for request_hash, request in self._requests.items():
            if agent_id and request.agentId != agent_id:
                continue
                
            responses = self._responses.get(request_hash, [])
            if not responses:
                pending.append({
                    "requestHash": request_hash,
                    "validatorAddress": request.validatorAddress,
                    "agentId": request.agentId,
                    "status": "pending",
                })
        
        return pending
    
    def get_supported_validators(self) -> Dict[str, str]:
        """Get list of supported validator types and their descriptions."""
        return self.VALIDATOR_TYPES.copy()

