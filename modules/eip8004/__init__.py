"""ERC-8004: Trustless Agents Implementation.

This module implements the ERC-8004 standard for trustless agent discovery and trust.
https://eips.ethereum.org/EIPS/eip-8004

Components:
- Identity Registry: On-chain NFT identity (ERC-721) for agent registration
- Reputation Registry: On-chain feedback system for agent scoring
- Validation Registry: On-chain validation verification (zkML, TEE, stake-secured)

Integration with existing systems:
- A2A: Agent Card endpoint linked in registration file
- x402: Payment proofs can enrich feedback signals
- MCP: Tool capabilities exposed via registration
"""

from .models import (
    EIP8004Config,
    RegistrationFile,
    Endpoint,
    Registration,
    FeedbackAuth,
    FeedbackEntry,
    ValidationRequestModel,
    ValidationResponseModel,
    ValidationStatus,
    ValidationSummary,
    ProofOfPayment,
    ReputationSummary,
)
from .registration import build_registration_file
from .reputation import ReputationManager
from .validation import ValidationManager
from .payment_proof import proof_from_settled_invoice
from .contracts import (
    IdentityRegistryContract,
    ReputationRegistryContract,
    ValidationRegistryContract,
)

__all__ = [
    # Config
    'EIP8004Config',
    # Models
    'RegistrationFile',
    'Endpoint',
    'Registration',
    'FeedbackAuth',
    'FeedbackEntry',
    'ValidationRequestModel',
    'ValidationResponseModel',
    'ValidationStatus',
    'ValidationSummary',
    'ProofOfPayment',
    'ReputationSummary',
    # Functions
    'build_registration_file',
    'proof_from_settled_invoice',
    # Managers
    'ReputationManager',
    'ValidationManager',
    # Contracts
    'IdentityRegistryContract',
    'ReputationRegistryContract',
    'ValidationRegistryContract',
]

