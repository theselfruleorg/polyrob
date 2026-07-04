from __future__ import annotations

"""Collab.Land integration service.

This service provides helper methods and bot actions to:  
1. Check whether a given blockchain address holds the required token(s) using the Collab.Land
   `access-control/check-roles` endpoint.  
2. Persist wallet → membership status inside the configured `database_manager`.  
3. Allow administrators to trigger a background refresh to keep the local cache in-sync
   with on-chain reality (users that dumped the token are revoked, new holders are granted).

The implementation conforms to the project-wide service conventions (inherits from
`BaseService`, defines `@action` decorated public endpoints, handles dependency
injection, proper life-cycle management, rate-limit handling, etc.).

NOTE: Collab.Land exposes a huge REST surface.  For the initial iteration we only
consume the token-gating endpoint:
    POST https://api.collab.land/access-control/check-roles
See https://dev.collab.land/docs/tutorials/token-gating-tutorial for details.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp
from pydantic import BaseModel, Field

from core.config import BotConfig
from core.exceptions import APIError, ConfigurationError, ToolError
from tools.base_tool import BaseTool, ToolStatus

__all__ = ["CollabLandTool"]


# ---------------------------------------------------------------------------
# Pydantic models for public actions
# ---------------------------------------------------------------------------


class CheckTokenParams(BaseModel):
    """Action parameters for `collabland_check_token`."""

    address: str = Field(..., description="EVM compatible wallet address to check")
    rules: Optional[List[Dict[str, Any]]] = Field(
        None,
        description=(
            "Optional explicit Token-Gating Rules overriding the defaults defined in "
            "bot configuration (see `collabland_rules`)."
        ),
    )


class AdminRefreshParams(BaseModel):
    """Parameters for the admin batch refresh action."""

    confirm: bool = Field(
        False,
        description="Extra safety switch. Must be `true` to actually run the refresh.",
    )


# ---------------------------------------------------------------------------
# Service implementation
# ---------------------------------------------------------------------------


class CollabLandTool(BaseTool):
    """Integration with Collab.Land token-gating endpoints."""

    # ---- Dependencies -----------------------------------------------------

    @property
    def required_services(self) -> Dict[str, str]:
        return {
            "rate_limit_manager": "Rate limit management",
            "database_manager": "Database persistence",
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
            "collabland_api_key": "Collab.Land API key",
            "collabland_rules": "Default Collab.Land token-gating rules (JSON list)",
        }

    # ---- Lifecycle --------------------------------------------------------

    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        super().__init__(name=name, config=config, container=container)

        self._session: Optional[aiohttp.ClientSession] = None
        self.base_url = getattr(config, "collabland_api_url", "https://api.collab.land")
        self._rules: List[Dict[str, Any]] = getattr(config, "collabland_rules", []) or []

        # Get all CollabLand configuration from BotConfig
        self._api_key = getattr(config, "collabland_api_key", "")
        self._client_id = getattr(config, "collabland_id", "")
        self._client_secret = getattr(config, "collabland_secret", "")
        
        # Handle compound API key format (clientId.secret)
        if self._api_key and '.' in self._api_key and not self._client_id:
            try:
                parts = self._api_key.split('.', 1)
                if len(parts) == 2:
                    self._client_id = parts[0]
                    self._client_secret = parts[1]
                    self.logger.info(f"Parsed compound API key: client_id={self._client_id[:5]}***, secret={self._client_secret[:5]}***")
            except Exception as e:
                self.logger.warning(f"Failed to parse compound API key: {e}")
        
        # Determine if service is properly configured
        self._enabled = bool(self._api_key)  # Only enable if we have an API key
        
        # Log the setup clearly
        if not self._api_key:
            self.logger.error("CRITICAL: Collab.Land API key missing – service disabled")
            self._enabled = False
        else:
            self.logger.info(f"CollabLand service configured:")
            self.logger.info(f"  - Base URL: {self.base_url}")
            self.logger.info(f"  - API Key: {self._api_key[:5]}*** (length: {len(self._api_key)})")
            if self._client_id:
                self.logger.info(f"  - Client ID: {self._client_id}")
            if self._client_secret:
                self.logger.info(f"  - Client Secret: {self._client_secret[:5]}***")
            self._enabled = True
            
        # Check contract address format if provided
        contract_address = getattr(config, "den_token_contract_address", None)
        if contract_address:
            if not contract_address.startswith("0x") or len(contract_address) != 42:
                self.logger.warning(f"Invalid ERC-721 contract address format: {contract_address}")
            else:
                self.logger.info(f"Using contract address: {contract_address}")
        else:
            self.logger.warning("Token contract address not configured - token verification may be unreliable")
            
        # Add default ERC721 rule if no rules configured
        if not self._rules and contract_address:
            self.logger.info("No CollabLand rules provided, creating default ERC721 rule")
            self._rules = [{
                "type": "ERC721",
                "chainId": 1,  # Ethereum mainnet
                "contractAddress": contract_address
            }]
            self.logger.info(f"Created default rule: {self._rules}")

    async def _initialize(self) -> None:
        """Create the shared aiohttp session and test connectivity."""
        # Ensure decorated actions are collected
        await super()._initialize()

        # Skip if not enabled
        if not self._enabled:
            self._status = ToolStatus.DISABLED
            self.logger.warning("CollabLand service disabled due to missing API key")
            return

        # Set up HTTP session
        headers = {
            "X-API-Key": self._api_key,  # Use the full API key as provided
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
        # Log headers for debugging (mask sensitive data)
        self.logger.debug(f"Setting up HTTP session with headers:")
        self.logger.debug(f"  - X-API-Key: {self._api_key[:10]}*** (full length: {len(self._api_key)})")
        self.logger.debug(f"  - Content-Type: application/json")
        self.logger.debug(f"  - Accept: application/json")
        
        # Create session
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        
        # Test connectivity
        try:
            # Simple test to check if API is reachable
            async with self._session.get(f"{self.base_url}/health") as response:
                if response.status == 200:
                    self._status = ToolStatus.HEALTHY
                    self.logger.info("CollabLand API connectivity verified")
                else:
                    self._status = ToolStatus.DEGRADED
                    self.logger.warning(f"CollabLand API returned status {response.status}")
        except Exception as e:
            self._status = ToolStatus.DEGRADED
            self.logger.error(f"Failed to connect to CollabLand API: {e}")
            # Don't disable the service, as it may recover later

    async def _cleanup(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            self.logger.debug("CollabLand HTTP session closed")

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------

    async def _check_roles(self, address: str, rules: List[Dict[str, Any]]) -> tuple[bool, Optional[str]]:
        """Return (has_token, token_id) if the address fulfils all provided rules."""
        # If service is disabled or not working, return False - no auto-pass
        if not self._enabled:
            self.logger.warning(f"CollabLand service disabled - token verification failed for wallet {address[:6]}...{address[-4:]}")
            return False, None
        
        # Ensure session exists
        if not self._session:
            self.logger.error("HTTP session not initialized - cannot verify token")
            return False, None
            
        try:
            # Prepare request payload
            payload = {
                "account": address,
                "rules": rules,
            }
            
            # Log the full request details for debugging
            self.logger.info(f"CollabLand API request: POST {self.base_url}/access-control/check-roles")
            self.logger.info(f"Request payload: {payload}")
            self.logger.debug(f"Request headers: X-API-Key: {self._api_key[:10]}*** (full: {len(self._api_key)} chars)")
            self.logger.debug(f"Full API key for debugging: {self._api_key}")  # Temporary debug - remove in production
            
            # Make API call to CollabLand
            url = f"{self.base_url}/access-control/check-roles"
            self.logger.info(f"Sending CollabLand API request to {url} for wallet {address[:6]}...{address[-4:]}")
            
            start_time = time.time()
            async with self._session.post(url, json=payload) as response:
                # Calculate response time
                response_time = round((time.time() - start_time) * 1000)
                
                # Log response status and headers at INFO level
                self.logger.info(f"CollabLand API response: status={response.status}, time={response_time}ms")
                self.logger.debug(f"CollabLand API response headers: {dict(response.headers)}")
                
                # Get response body for detailed logging
                response_body = await response.text()
                self.logger.debug(f"CollabLand API response body: {response_body}")
                
                # Parse response
                if response.status == 200:
                    try:
                        data = await response.json()
                        self.logger.info(f"CollabLand API returned JSON response: {data}")
                        
                        # Extract relevant information
                        has_token = data.get("hasAccess", False)
                        
                        # Try to extract token ID if available
                        token_id = None
                        if has_token:
                            # Access token details from response
                            token_details = data.get("tokenBalances", [])
                            if token_details:
                                # Take the first token ID if available
                                first_token = token_details[0]
                                token_id = first_token.get("tokenId")
                                self.logger.info(f"Found token ID: {token_id}")
                        
                        return has_token, token_id
                    except Exception as parse_error:
                        self.logger.error(f"Error parsing JSON response: {parse_error}")
                        self.logger.error(f"Raw response: {response_body}")
                        # Don't mask parsing errors - let them bubble up
                        raise Exception(f"Failed to parse CollabLand API response: {parse_error}")
                else:
                    # Handle error responses - raise exceptions for authentication errors instead of returning False
                    error_text = await response.text()
                    self.logger.error(f"CollabLand API error: {response.status} - {error_text}")
                    
                    # Parse error response to get detailed error message
                    error_details = error_text
                    try:
                        import json
                        error_json = json.loads(error_text)
                        if isinstance(error_json, dict) and "error" in error_json:
                            error_info = error_json["error"]
                            if isinstance(error_info, dict):
                                error_details = error_info.get("message", error_text)
                    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                        # If we can't parse the error JSON, use the raw text
                        pass
                    
                    # Raise specific exceptions for authentication errors to trigger Alchemy fallback
                    if response.status == 401 or response.status == 403:
                        self.logger.error("CollabLand API authentication error - check API key")
                        raise Exception(f"CollabLand authentication error (HTTP {response.status}): {error_details}")
                    elif response.status == 429:
                        self.logger.warning("CollabLand API rate limited - consider implementing backoff")
                        raise Exception(f"CollabLand rate limit error (HTTP {response.status}): {error_details}")
                    else:
                        # For other errors, also raise exceptions to trigger fallback
                        raise Exception(f"CollabLand API error (HTTP {response.status}): {error_details}")
                    
        except Exception as e:
            # Only catch and log unexpected exceptions, but re-raise them so they can trigger fallback
            self.logger.error(f"Error checking token for {address}: {e}")
            # Log the full exception traceback for better debugging
            import traceback
            self.logger.error(f"Exception traceback: {traceback.format_exc()}")
            
            # Check if this is an authentication error we should propagate
            error_str = str(e).lower()
            if any(keyword in error_str for keyword in ["authentication", "unauthorized", "invalid client", "forbidden", "api key"]):
                # Re-raise authentication errors to trigger Alchemy fallback
                raise e
            elif "rate limit" in error_str or "429" in error_str:
                # Re-raise rate limit errors to trigger Alchemy fallback
                raise e
            else:
                # For other unexpected errors, also re-raise to trigger fallback
                raise e

    # ---------------------------------------------------------------------
    # Database helpers
    # ---------------------------------------------------------------------

    async def _update_user_token_status(self, user_id: str, has_token: bool, token_id: Optional[str] = None) -> None:
        """Persist the `has_token` flag inside database_manager.

        The concrete schema is application-specific.  We assume a "users" table with
        at least columns `(id, has_token)` and upsert semantics exposed through
        `database_manager.set_user_flag`.
        """
        db = self.database
        if not db:
            self.logger.warning("database_manager missing – cannot persist token status")
            return

        try:
            if hasattr(db, "set_user_flag"):
                await db.set_user_flag(user_id, "has_token", has_token)
                if token_id:
                    await db.set_user_flag(user_id, "token_id", token_id)
            else:
                # Fallback: generic upsert API
                await db.upsert("users", {"id": user_id, "has_token": has_token})
                if token_id:
                    await db.upsert("users", {"id": user_id, "token_id": token_id})
        except Exception as exc:
            self.logger.error(f"Failed to update DB for user {user_id}: {exc}")

    # ---------------------------------------------------------------------
    # Public actions
    # ---------------------------------------------------------------------

    @BaseTool.action(
        "Check if the provided wallet address owns the required Collab.Land token(s)",
        param_model=CheckTokenParams,
    )
    async def collabland_check_token(self, params: CheckTokenParams):
        """Return a human-readable message and persist the result in DB."""
        await self.ensure_initialized()

        if not self._enabled:
            self.logger.error("CollabLand service is disabled (missing API key) - token verification failed")
            return {
                "status": "error", 
                "has_token": False, 
                "token_id": None, 
                "message": "Token verification service is currently unavailable. Please contact an administrator."
            }

        rules = params.rules or self._rules
        if not rules:
            self.logger.warning("No token-gating rules specified - verification will fail")
            return {
                "status": "error",
                "has_token": False,
                "token_id": None,
                "message": "No token-gating rules available. Please contact an administrator."
            }

        # Normalise address
        address = params.address.strip().lower()

        try:
            # Log the request details at INFO level for better visibility
            self.logger.info(f"CollabLand token verification started for wallet: {address[:6]}...{address[-4:]}")
            self.logger.info(f"Using rules: {rules}")
            
            has_token, token_id = await self._check_roles(address, rules)

            # Note: Database updates are handled by the calling service (den_onboarding_handler)
            # We don't update the DB here to avoid table conflicts

            # Mask address for user-facing messages
            masked_address = f"{address[:6]}...{address[-4:]}"
            
            if has_token:
                message = f"✔️ Address `{masked_address}` holds the required token(s)."
                if token_id:
                    message += f" Token ID: {token_id}"
                self.logger.info(f"Token verification SUCCESS for {masked_address}: has_token=True, token_id={token_id or 'None'}")
            else:
                message = f"❌ Address `{masked_address}` does NOT meet the token-gating requirements."
                self.logger.info(f"Token verification FAILED for {masked_address}: has_token=False")
            
            # Create standardized response format
            result = {
                "status": "success", 
                "has_token": has_token, 
                "token_id": token_id, 
                "message": message
            }
            
            # Log full result at INFO level
            self.logger.info(f"CollabLand verification complete: {result}")
            
            return result
        except Exception as e:
            self.logger.error(f"Failed to check token for {address}: {e}")
            # Get full exception details
            import traceback
            self.logger.error(f"Exception traceback: {traceback.format_exc()}")
            return {
                "status": "error",
                "has_token": False,
                "token_id": None,
                "message": f"Error checking token: {str(e)}"
            }

    @BaseTool.action(
        "Admin: batch refresh token status for all known users",
        param_model=AdminRefreshParams,
    )
    async def collabland_admin_refresh(self, params: AdminRefreshParams):
        """Iterate over all users stored in DB and refresh their token status."""
        await self.ensure_initialized()

        if not params.confirm:
            return (
                "Confirmation missing – set `confirm=true` in parameters to execute the "
                "batch refresh."
            )

        db = self.database
        if not db or not hasattr(db, "get_all_users"):
            raise ToolError("database_manager does not expose get_all_users() API")

        users: List[Dict[str, Any]] = await db.get_all_users()
        total, updated = len(users), 0

        for user in users:
            address = user.get("id") or user.get("wallet") or user.get("address")
            if not address:
                continue

            try:
                has_token, token_id = await self._check_roles(address, self._rules)
                current_flag = bool(user.get("has_token"))
                if has_token != current_flag:
                    await self._update_user_token_status(address, has_token)
                    updated += 1
            except Exception as exc:
                self.logger.warning(f"Refresh failed for {address}: {exc}")

        return f"Batch refresh completed – {updated}/{total} records updated." 