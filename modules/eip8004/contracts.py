"""ERC-8004 Contract Interfaces.

Interface definitions for interacting with the on-chain registries.
Uses web3.py for contract interactions.
"""

import os
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


# =============================================================================
# Contract ABIs (Minimal for ERC-8004)
# =============================================================================

IDENTITY_REGISTRY_ABI = [
    # ERC-721 standard
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "tokenURI",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    # ERC-8004 extensions
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "key", "type": "string"}
        ],
        "name": "getMetadata",
        "outputs": [{"name": "", "type": "bytes"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "tokenURI", "type": "string"},
            {"name": "metadata", "type": "tuple[]", "components": [
                {"name": "key", "type": "string"},
                {"name": "value", "type": "bytes"}
            ]}
        ],
        "name": "register",
        "outputs": [{"name": "agentId", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    # Events
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": False, "name": "tokenURI", "type": "string"},
            {"indexed": True, "name": "owner", "type": "address"}
        ],
        "name": "Registered",
        "type": "event"
    },
]

REPUTATION_REGISTRY_ABI = [
    {
        "inputs": [],
        "name": "getIdentityRegistry",
        "outputs": [{"name": "identityRegistry", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "feedbackAuth", "type": "bytes"},
            {"name": "score", "type": "uint8"},
            {"name": "tag1", "type": "bytes32"},
            {"name": "tag2", "type": "bytes32"},
            {"name": "fileUri", "type": "string"},
            {"name": "fileHash", "type": "bytes32"}
        ],
        "name": "submitFeedback",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "clientAddress", "type": "address"},
            {"name": "tag1", "type": "bytes32"},
            {"name": "tag2", "type": "bytes32"}
        ],
        "name": "getAggregatedFeedback",
        "outputs": [
            {"name": "count", "type": "uint64"},
            {"name": "avgScore", "type": "uint8"},
            {"name": "scores", "type": "uint8[]"},
            {"name": "tag1s", "type": "bytes32[]"},
            {"name": "tag2s", "type": "bytes32[]"},
            {"name": "revokedStatuses", "type": "bool[]"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "name": "getClients",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function"
    },
    # Events
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": True, "name": "clientAddress", "type": "address"},
            {"indexed": False, "name": "score", "type": "uint8"},
            {"indexed": True, "name": "feedbackIndex", "type": "uint64"}
        ],
        "name": "FeedbackSubmitted",
        "type": "event"
    },
]

VALIDATION_REGISTRY_ABI = [
    {
        "inputs": [],
        "name": "getIdentityRegistry",
        "outputs": [{"name": "identityRegistry", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "validatorAddress", "type": "address"},
            {"name": "agentId", "type": "uint256"},
            {"name": "requestUri", "type": "string"},
            {"name": "requestHash", "type": "bytes32"}
        ],
        "name": "validationRequest",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "requestHash", "type": "bytes32"},
            {"name": "response", "type": "uint8"},
            {"name": "responseUri", "type": "string"},
            {"name": "responseHash", "type": "bytes32"},
            {"name": "tag", "type": "bytes32"}
        ],
        "name": "validationResponse",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "requestHash", "type": "bytes32"}],
        "name": "getValidationStatus",
        "outputs": [
            {"name": "validatorAddress", "type": "address"},
            {"name": "agentId", "type": "uint256"},
            {"name": "response", "type": "uint8"},
            {"name": "tag", "type": "bytes32"},
            {"name": "lastUpdate", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "validatorAddresses", "type": "address[]"},
            {"name": "tag", "type": "bytes32"}
        ],
        "name": "getSummary",
        "outputs": [
            {"name": "count", "type": "uint64"},
            {"name": "avgResponse", "type": "uint8"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    # Events
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "validatorAddress", "type": "address"},
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": False, "name": "requestUri", "type": "string"},
            {"indexed": True, "name": "requestHash", "type": "bytes32"}
        ],
        "name": "ValidationRequest",
        "type": "event"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "validatorAddress", "type": "address"},
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": True, "name": "requestHash", "type": "bytes32"},
            {"indexed": False, "name": "response", "type": "uint8"},
            {"indexed": False, "name": "responseUri", "type": "string"},
            {"indexed": False, "name": "tag", "type": "bytes32"}
        ],
        "name": "ValidationResponse",
        "type": "event"
    },
]


# =============================================================================
# Contract Interfaces
# =============================================================================

class BaseContract:
    """Base class for contract interactions."""
    
    def __init__(self, address: str, abi: List[Dict], chain_id: int = 8453):
        """Initialize contract interface.
        
        Args:
            address: Contract address
            abi: Contract ABI
            chain_id: Chain ID (default: Base)
        """
        self.address = address
        self.abi = abi
        self.chain_id = chain_id
        self._web3 = None
        self._contract = None
    
    def _get_web3(self):
        """Get web3 instance lazily."""
        if self._web3 is None:
            try:
                from web3 import Web3
                
                # Get RPC URL based on chain
                rpc_urls = {
                    1: os.environ.get("ETHEREUM_RPC_URL"),
                    8453: os.environ.get("BASE_RPC_URL"),
                    137: os.environ.get("POLYGON_RPC_URL"),
                    42161: os.environ.get("ARBITRUM_RPC_URL"),
                }
                
                rpc_url = rpc_urls.get(self.chain_id)
                if not rpc_url:
                    raise ValueError(f"No RPC URL configured for chain {self.chain_id}")
                
                self._web3 = Web3(Web3.HTTPProvider(rpc_url))
                self._contract = self._web3.eth.contract(
                    address=Web3.to_checksum_address(self.address),
                    abi=self.abi
                )
            except ImportError:
                logger.warning("web3 not installed, contract interactions disabled")
                return None
        
        return self._web3
    
    def _get_contract(self):
        """Get contract instance."""
        self._get_web3()
        return self._contract
    
    @property
    def is_available(self) -> bool:
        """Check if contract interactions are available."""
        return self._get_web3() is not None and self._contract is not None


class IdentityRegistryContract(BaseContract):
    """Interface for the ERC-8004 Identity Registry (ERC-721)."""
    
    def __init__(self, address: str, chain_id: int = 8453):
        super().__init__(address, IDENTITY_REGISTRY_ABI, chain_id)
    
    async def get_token_uri(self, agent_id: int) -> Optional[str]:
        """Get the tokenURI for an agent.
        
        Args:
            agent_id: Agent's tokenId
            
        Returns:
            Token URI (points to registration file)
        """
        contract = self._get_contract()
        if not contract:
            return None
        
        try:
            return contract.functions.tokenURI(agent_id).call()
        except Exception as e:
            logger.error(f"Failed to get tokenURI: {e}")
            return None
    
    async def get_owner(self, agent_id: int) -> Optional[str]:
        """Get the owner of an agent.
        
        Args:
            agent_id: Agent's tokenId
            
        Returns:
            Owner address
        """
        contract = self._get_contract()
        if not contract:
            return None
        
        try:
            return contract.functions.ownerOf(agent_id).call()
        except Exception as e:
            logger.error(f"Failed to get owner: {e}")
            return None
    
    async def get_metadata(self, agent_id: int, key: str) -> Optional[bytes]:
        """Get on-chain metadata for an agent.
        
        Args:
            agent_id: Agent's tokenId
            key: Metadata key (e.g., "agentWallet", "agentName")
            
        Returns:
            Metadata value as bytes
        """
        contract = self._get_contract()
        if not contract:
            return None
        
        try:
            return contract.functions.getMetadata(agent_id, key).call()
        except Exception as e:
            logger.error(f"Failed to get metadata: {e}")
            return None


class ReputationRegistryContract(BaseContract):
    """Interface for the ERC-8004 Reputation Registry."""
    
    def __init__(self, address: str, chain_id: int = 8453):
        super().__init__(address, REPUTATION_REGISTRY_ABI, chain_id)
    
    async def get_aggregated_feedback(
        self,
        agent_id: int,
        client_address: Optional[str] = None,
        tag1: Optional[bytes] = None,
        tag2: Optional[bytes] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get aggregated feedback for an agent.
        
        Args:
            agent_id: Agent's tokenId
            client_address: Filter by client
            tag1: Filter by tag1
            tag2: Filter by tag2
            
        Returns:
            Aggregated feedback data
        """
        contract = self._get_contract()
        if not contract:
            return None
        
        try:
            from web3 import Web3
            
            client = client_address or "0x0000000000000000000000000000000000000000"
            t1 = tag1 or b'\x00' * 32
            t2 = tag2 or b'\x00' * 32
            
            result = contract.functions.getAggregatedFeedback(
                agent_id,
                Web3.to_checksum_address(client),
                t1,
                t2
            ).call()
            
            return {
                "count": result[0],
                "avgScore": result[1],
                "scores": list(result[2]),
                "tag1s": list(result[3]),
                "tag2s": list(result[4]),
                "revokedStatuses": list(result[5]),
            }
        except Exception as e:
            logger.error(f"Failed to get aggregated feedback: {e}")
            return None
    
    async def get_clients(self, agent_id: int) -> List[str]:
        """Get list of clients who provided feedback.
        
        Args:
            agent_id: Agent's tokenId
            
        Returns:
            List of client addresses
        """
        contract = self._get_contract()
        if not contract:
            return []
        
        try:
            return contract.functions.getClients(agent_id).call()
        except Exception as e:
            logger.error(f"Failed to get clients: {e}")
            return []


class ValidationRegistryContract(BaseContract):
    """Interface for the ERC-8004 Validation Registry."""
    
    def __init__(self, address: str, chain_id: int = 8453):
        super().__init__(address, VALIDATION_REGISTRY_ABI, chain_id)
    
    async def get_validation_status(
        self,
        request_hash: bytes,
    ) -> Optional[Dict[str, Any]]:
        """Get validation status for a request.
        
        Args:
            request_hash: Hash of the validation request
            
        Returns:
            Validation status data
        """
        contract = self._get_contract()
        if not contract:
            return None
        
        try:
            result = contract.functions.getValidationStatus(request_hash).call()
            
            return {
                "validatorAddress": result[0],
                "agentId": result[1],
                "response": result[2],
                "tag": result[3],
                "lastUpdate": result[4],
            }
        except Exception as e:
            logger.error(f"Failed to get validation status: {e}")
            return None
    
    async def get_summary(
        self,
        agent_id: int,
        validator_addresses: Optional[List[str]] = None,
        tag: Optional[bytes] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get validation summary for an agent.
        
        Args:
            agent_id: Agent's tokenId
            validator_addresses: Filter by validators
            tag: Filter by tag
            
        Returns:
            Summary with count and average response
        """
        contract = self._get_contract()
        if not contract:
            return None
        
        try:
            from web3 import Web3
            
            validators = validator_addresses or []
            validators_checksum = [Web3.to_checksum_address(v) for v in validators]
            t = tag or b'\x00' * 32
            
            result = contract.functions.getSummary(
                agent_id,
                validators_checksum,
                t
            ).call()
            
            return {
                "count": result[0],
                "avgResponse": result[1],
            }
        except Exception as e:
            logger.error(f"Failed to get validation summary: {e}")
            return None

