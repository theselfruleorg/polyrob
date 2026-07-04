"""Alchemy NFT API integration service.

This service provides helper methods and bot actions to check whether a given 
blockchain address holds the required token(s) using the Alchemy NFT API's 
`getNFTsForOwner` endpoint.

This service acts as a fallback for the CollabLand service when CollabLand 
is unavailable or experiencing issues.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp
from pydantic import BaseModel, Field

from core.config import BotConfig
from core.exceptions import APIError, ConfigurationError, ToolError
from tools.base_tool import BaseTool, ToolStatus

__all__ = ["AlchemyTool"]


# ---------------------------------------------------------------------------
# Pydantic models for public actions
# ---------------------------------------------------------------------------


class CheckTokenParams(BaseModel):
    """Action parameters for `alchemy_check_token`."""

    address: str = Field(..., description="EVM compatible wallet address to check")
    contract_address: Optional[str] = Field(
        None,
        description="NFT contract address to check ownership for"
    )


# ---------------------------------------------------------------------------
# Service implementation
# ---------------------------------------------------------------------------


class AlchemyTool(BaseTool):
    """Integration with Alchemy NFT API for token ownership verification."""

    # ---- Dependencies -----------------------------------------------------

    @property
    def required_services(self) -> Dict[str, str]:
        return {
            "rate_limit_manager": "Rate limit management",
        }

    @property
    def optional_services(self) -> Dict[str, str]:
        return {
            "cache_manager": "Optional cache layer",
        }

    # ---- Configuration ----------------------------------------------------

    @property
    def required_config(self) -> Dict[str, str]:
        return {
            "alchemy_api_key": "Alchemy API key",
        }

    # ---- Lifecycle --------------------------------------------------------

    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        super().__init__(name=name, config=config, container=container)

        self._session: Optional[aiohttp.ClientSession] = None
        
        # Get Alchemy configuration from BotConfig
        self._api_key = getattr(config, "alchemy_api_key", "")
        self.base_url = getattr(config, "alchemy_api_url", "https://eth-mainnet.g.alchemy.com")
        
        # Get NFT contract address from config
        self._default_contract_address = getattr(config, "den_token_contract_address", "")
        
        # Determine if service is properly configured
        self._enabled = bool(self._api_key)
        
        # Log the setup clearly
        if not self._api_key:
            self.logger.error("CRITICAL: Alchemy API key missing – service disabled")
            self._enabled = False
        else:
            self.logger.info(f"Alchemy service configured:")
            self.logger.info(f"  - Base URL: {self.base_url}")
            self.logger.info(f"  - API Key: ******** (length: {len(self._api_key)})")
            self._enabled = True
            
        # Check contract address format if provided
        if self._default_contract_address:
            if not self._default_contract_address.startswith("0x") or len(self._default_contract_address) != 42:
                self.logger.warning(f"Invalid ERC-721 contract address format: {self._default_contract_address}")
            else:
                self.logger.info(f"Using default contract address: {self._default_contract_address}")
        else:
            self.logger.warning("Token contract address not configured - will need to be provided in requests")

    async def _initialize(self) -> None:
        """Create the shared aiohttp session and test connectivity."""
        # Ensure decorated actions are collected
        await super()._initialize()

        # Skip if not enabled
        if not self._enabled:
            self._status = ToolStatus.FAILED
            self.logger.warning("Alchemy service disabled due to missing API key")
            return

        # Set up HTTP session
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
        # Create session
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        
        # Test connectivity with a simple request
        try:
            # Test with a known address that should return quickly
            test_url = f"{self.base_url}/nft/v3/{self._api_key}/getNFTsForOwner"
            test_params = {
                "owner": "0x0000000000000000000000000000000000000000",  # Zero address for testing
                "pageSize": "1",
                "withMetadata": "false"
            }
            
            async with self._session.get(f"{test_url}?{urlencode(test_params)}") as response:
                if response.status in [200, 400]:  # 400 is expected for zero address, but means API is responding
                    self._status = ToolStatus.HEALTHY
                    self.logger.info("Alchemy API connectivity verified")
                else:
                    self._status = ToolStatus.DEGRADED
                    self.logger.warning(f"Alchemy API returned status {response.status}")
        except Exception as e:
            self._status = ToolStatus.DEGRADED
            self.logger.error(f"Failed to connect to Alchemy API: {e}")
            # Don't disable the service, as it may recover later

    async def _cleanup(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            self.logger.debug("Alchemy HTTP session closed")

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------

    async def _check_nft_ownership(self, address: str, contract_address: str) -> tuple[bool, list[str], int]:
        """Check if address owns any NFTs from the specified contract.

        Returns:
            tuple: (has_token, token_ids, total_count) - has_token indicates ownership,
                   token_ids is list of ALL owned token IDs, total_count is total owned
        """
        # If service is disabled, return empty
        if not self._enabled:
            self.logger.warning(f"Alchemy service disabled - token verification failed for wallet {address[:6]}...{address[-4:]}")
            return False, [], 0

        # Ensure session exists
        if not self._session:
            self.logger.error("HTTP session not initialized - cannot verify token")
            return False, [], 0
            
        try:
            # Prepare request URL and parameters
            url = f"{self.base_url}/nft/v3/{self._api_key}/getNFTsForOwner"
            params = {
                "owner": address,
                "contractAddresses[]": contract_address,
                "withMetadata": "false",  # We don't need metadata, just ownership
                "pageSize": "100",  # Get all tokens (up to 100) to track each token ID
            }
            
            # Log the request details for debugging
            self.logger.info(f"Alchemy API request: GET {url}")
            self.logger.info(f"Request params: {params}")
            
            # Make API call to Alchemy
            start_time = time.time()
            async with self._session.get(url, params=params) as response:
                # Calculate response time
                response_time = round((time.time() - start_time) * 1000)
                
                # Log response status
                self.logger.info(f"Alchemy API response: status={response.status}, time={response_time}ms")
                
                if response.status == 200:
                    try:
                        data = await response.json()
                        self.logger.debug(f"Alchemy API response data: {data}")

                        # Check if user owns any NFTs from this contract
                        owned_nfts = data.get("ownedNfts", [])
                        total_count = data.get("totalCount", 0)

                        has_token = total_count > 0 and len(owned_nfts) > 0
                        token_ids = []

                        if has_token and owned_nfts:
                            # Get ALL token IDs (critical for bonus tracking)
                            for nft in owned_nfts:
                                token_id = nft.get("tokenId")
                                if token_id:
                                    token_ids.append(token_id)
                            self.logger.info(f"Found token ownership: token_ids={token_ids}, totalCount={total_count}")

                        return has_token, token_ids, total_count

                    except Exception as parse_error:
                        self.logger.error(f"Error parsing JSON response: {parse_error}")
                        response_text = await response.text()
                        self.logger.error(f"Raw response: {response_text}")
                        return False, [], 0
                else:
                    # Handle error responses
                    error_text = await response.text()
                    self.logger.error(f"Alchemy API error: {response.status} - {error_text}")

                    # Handle specific error cases
                    if response.status == 429:
                        self.logger.warning("Alchemy API rate limited - consider implementing backoff")
                    elif response.status == 401 or response.status == 403:
                        self.logger.error("Alchemy API authentication error - check API key")

                    return False, [], 0

        except Exception as e:
            self.logger.error(f"Error checking token for {address}: {e}")
            # Log the full exception traceback for better debugging
            import traceback
            self.logger.error(f"Exception traceback: {traceback.format_exc()}")
            return False, [], 0

    # ---------------------------------------------------------------------
    # Public actions
    # ---------------------------------------------------------------------

    @BaseTool.action(
        "Check if the provided wallet address owns the required NFT token using Alchemy API",
        param_model=CheckTokenParams,
    )
    async def alchemy_check_token(self, params: CheckTokenParams):
        """Check token ownership using Alchemy API and return standardized response."""
        await self.ensure_initialized()

        if not self._enabled:
            self.logger.error("Alchemy service is disabled (missing API key) - token verification failed")
            return {
                "status": "error", 
                "has_token": False, 
                "token_id": None, 
                "message": "Alchemy token verification service is currently unavailable. Please contact an administrator."
            }

        # Use provided contract address or default
        contract_address = params.contract_address or self._default_contract_address
        if not contract_address:
            self.logger.error("No contract address provided and no default configured")
            return {
                "status": "error",
                "has_token": False,
                "token_id": None,
                "message": "No NFT contract address configured. Please contact an administrator."
            }

        # Normalize address
        address = params.address.strip().lower()

        try:
            # Log the request details at INFO level for better visibility
            self.logger.info(f"Alchemy token verification started for wallet: {address[:6]}...{address[-4:]}")
            self.logger.info(f"Using contract: {contract_address}")

            has_token, token_ids, total_count = await self._check_nft_ownership(address, contract_address)

            # Mask address for user-facing messages
            masked_address = f"{address[:6]}...{address[-4:]}"

            if has_token:
                message = f"Address `{masked_address}` holds {total_count} DEN token(s)."
                if token_ids:
                    message += f" Token IDs: {', '.join(token_ids[:5])}"
                    if len(token_ids) > 5:
                        message += f"... (+{len(token_ids) - 5} more)"
                self.logger.info(f"Token verification SUCCESS for {masked_address}: has_token=True, count={total_count}, ids={token_ids}")
            else:
                message = f"Address `{masked_address}` does NOT own any DEN tokens."
                self.logger.info(f"Token verification FAILED for {masked_address}: has_token=False")

            # Create standardized response format with ALL token IDs
            result = {
                "status": "success",
                "has_token": has_token,
                "token_count": total_count,
                "token_ids": token_ids,  # List of ALL token IDs (for bonus tracking)
                "token_id": token_ids[0] if token_ids else None,  # Backward compat
                "contract_address": contract_address,
                "message": message,
                "service": "alchemy"
            }

            # Log full result at INFO level
            self.logger.info(f"Alchemy verification complete: {result}")

            return result

        except Exception as e:
            self.logger.error(f"Failed to check token for {address}: {e}")
            # Get full exception details
            import traceback
            self.logger.error(f"Exception traceback: {traceback.format_exc()}")
            return {
                "status": "error",
                "has_token": False,
                "token_count": 0,
                "token_ids": [],
                "token_id": None,
                "contract_address": contract_address,
                "message": f"Error checking token via Alchemy: {str(e)}",
                "service": "alchemy"
            }

    @BaseTool.action(
        "Test Alchemy API connectivity and configuration",
        param_model=None,
    )
    async def alchemy_test_connection(self):
        """Test the Alchemy API connection and configuration."""
        await self.ensure_initialized()
        
        if not self._enabled:
            return {
                "status": "error",
                "message": "Alchemy service is disabled (missing API key)"
            }
            
        try:
            # Test with a known address
            test_address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"  # vitalik.eth
            
            url = f"{self.base_url}/nft/v3/{self._api_key}/getNFTsForOwner"
            params = {
                "owner": test_address,
                "pageSize": "1",
                "withMetadata": "false"
            }
            
            start_time = time.time()
            async with self._session.get(url, params=params) as response:
                response_time = round((time.time() - start_time) * 1000)
                
                if response.status == 200:
                    data = await response.json()
                    total_count = data.get("totalCount", 0)
                    
                    return {
                        "status": "success",
                        "message": f"Alchemy API is working correctly. Test query returned {total_count} NFTs.",
                        "response_time_ms": response_time,
                        "api_endpoint": url,
                        "service_status": self._status.value
                    }
                else:
                    error_text = await response.text()
                    return {
                        "status": "error",
                        "message": f"Alchemy API returned status {response.status}: {error_text}",
                        "response_time_ms": response_time
                    }
                    
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to test Alchemy API: {str(e)}"
            } 