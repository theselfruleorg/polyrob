"""ERC-8004: Trustless Agents Implementation.

This module implements the ERC-8004 standard for trustless agent discovery and trust.
https://eips.ethereum.org/EIPS/eip-8004

Components:
- Identity Registry: On-chain NFT identity (ERC-721) for agent registration
- Reputation Registry: read on-chain by core/wallet/erc8004_reputation.py (no local feedback API)
- Validation Registry: On-chain validation verification (zkML, TEE, stake-secured)

Integration with existing systems:
- A2A: Agent Card endpoint linked in registration file
- MCP: Tool capabilities exposed via registration
"""

from .models import (
    EIP8004Config,
    RegistrationFile,
    Endpoint,
    Registration,
    ValidationRequestModel,
    ValidationResponseModel,
    ValidationStatus,
    ValidationSummary,
)
from .registration import build_registration_file
from .validation import ValidationManager
from .contracts import (
    IdentityRegistryContract,
    ValidationRegistryContract,
)

__all__ = [
    # Config
    'EIP8004Config',
    # Models
    'RegistrationFile',
    'Endpoint',
    'Registration',
    'ValidationRequestModel',
    'ValidationResponseModel',
    'ValidationStatus',
    'ValidationSummary',
    # Functions
    'build_registration_file',
    # Manager
    'ValidationManager',
    # Contracts
    'IdentityRegistryContract',
    'ValidationRegistryContract',
]

