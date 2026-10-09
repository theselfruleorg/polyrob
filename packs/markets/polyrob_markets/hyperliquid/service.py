"""
Hyperliquid Tool Service

Provides trading capabilities for Hyperliquid perpetuals and spot markets.
"""

import asyncio
import contextvars
import time
import httpx
from typing import Optional, Dict, Any, List, Tuple
from pydantic import BaseModel, Field

from tools.base_tool import BaseTool, ToolStatus
from tools.credential_cache import UserCredentialCacheMixin
from polyrob_markets.hyperliquid.models import (
    HyperliquidCredentials,
    TradingLimits,
    ExecutionResult,
    MAINNET_API_URL,
    TESTNET_API_URL,
    MIN_ORDER_VALUE_USD,
)
from core.logging import get_component_logger

#: The worst Hyperliquid fee an order can pay, as a fraction of its notional (the base-tier
#: spot taker rate is 0.07%; perps are lower). The fee leaves the margin account like the
#: order itself, so the spend caps are charged notional + this, never the notional alone.
HL_WORST_FEE_RATE = 0.001


def _with_fee(order_value_usd: float) -> float:
    return float(order_value_usd) * (1.0 + HL_WORST_FEE_RATE)

# Official SDK — imported LAZILY: hyperliquid.exchange drags eth_account (~100 ms)
# into every process boot via tools/__init__. _has_sdk() performs the one real
# import on first call and populates the module globals the call sites read.
Info = None
Exchange = None
HAS_SDK = False
_SDK_LOADED = False


def _has_sdk() -> bool:
    """True iff the official hyperliquid SDK is importable (loads on first call)."""
    global Info, Exchange, HAS_SDK, _SDK_LOADED
    if not _SDK_LOADED:
        _SDK_LOADED = True
        try:
            from hyperliquid.info import Info as _Info
            from hyperliquid.exchange import Exchange as _Exchange
            Info, Exchange = _Info, _Exchange
            HAS_SDK = True
        except ImportError:
            HAS_SDK = False
    return HAS_SDK


# =============================================================================
# Parameter Models (Pydantic)
# =============================================================================

class EmptyParams(BaseModel):
    """Empty parameters for actions that don't need input"""
    pass


class SearchMarketsParams(BaseModel):
    """Parameters for searching markets"""
    query: Optional[str] = Field(None, description="Search query (coin name/symbol)")
    market_type: str = Field("perpetual", description="Market type: 'perpetual' or 'spot'")
    limit: int = Field(20, ge=1, le=100, description="Max results to return")


class GetPriceParams(BaseModel):
    """Parameters for getting current price"""
    coin: str = Field(..., description="Coin symbol (e.g., 'BTC', 'ETH')")


class GetOrderbookParams(BaseModel):
    """Parameters for getting orderbook"""
    coin: str = Field(..., description="Coin symbol")
    depth: int = Field(10, ge=1, le=100, description="Orderbook depth")


class GetFundingParams(BaseModel):
    """Parameters for getting funding rate"""
    coin: str = Field(..., description="Coin symbol")


class PlaceLimitOrderParams(BaseModel):
    """Parameters for placing a limit order"""
    coin: str = Field(..., description="Coin symbol")
    is_buy: bool = Field(..., description="True for buy/long, False for sell/short")
    size: float = Field(..., gt=0, description="Order size in base asset")
    price: float = Field(..., gt=0, description="Limit price")
    max_usd: float = Field(..., gt=0, allow_inf_nan=False,
                          description="Owner-approved maximum USD notional, checked against live pricing")
    reduce_only: bool = Field(False, description="Reduce-only order")
    post_only: bool = Field(False, description="Post-only (maker) order")
    client_order_id: Optional[str] = Field(None, description="Custom order ID")


class PlaceMarketOrderParams(BaseModel):
    """Parameters for placing a market order"""
    coin: str = Field(..., description="Coin symbol")
    is_buy: bool = Field(..., description="True for buy/long, False for sell/short")
    size: float = Field(..., gt=0, description="Order size in base asset")
    max_usd: float = Field(..., gt=0, allow_inf_nan=False,
                          description="Owner-approved maximum USD notional, including worst-case slippage")
    slippage: float = Field(0.05, ge=0, le=0.2, description="Max slippage (0.05 = 5%)")
    reduce_only: bool = Field(False, description="Reduce-only order")


class CancelOrderParams(BaseModel):
    """Parameters for canceling an order"""
    coin: str = Field(..., description="Coin symbol")
    order_id: int = Field(..., description="Order ID to cancel")


class CancelAllOrdersParams(BaseModel):
    """Parameters for canceling all orders"""
    coin: Optional[str] = Field(None, description="Optional: cancel only for this coin")


class UpdateLeverageParams(BaseModel):
    """Parameters for updating leverage"""
    coin: str = Field(..., description="Coin symbol")
    leverage: int = Field(..., ge=1, le=50, description="New leverage value")
    is_cross: bool = Field(True, description="Cross margin (True) or Isolated (False)")


class GetFillsParams(BaseModel):
    """Parameters for getting fill history"""
    limit: int = Field(50, ge=1, le=500, description="Max fills to return")


class GetOrderHistoryParams(BaseModel):
    """Parameters for getting order history"""
    limit: int = Field(50, ge=1, le=500, description="Max orders to return")


# =============================================================================
# Service Class
# =============================================================================

class HyperliquidTool(UserCredentialCacheMixin, BaseTool):
    """
    Hyperliquid trading tool for perpetuals and spot markets.

    Provides market data, portfolio management, and trading capabilities
    with built-in safety limits and audit logging.
    """

    tool_id = "hyperliquid"
    name = "hyperliquid"
    description = "Hyperliquid perpetual futures and spot trading"

    def __init__(
        self,
        name: str = "hyperliquid",
        config: Any = None,
        container: Any = None,
    ):
        super().__init__(name=name, config=config, container=container)
        self.db = None  # HyperliquidDBHandler
        self._user_id = None  # CR-M14: task-local, see the property
        self._credentials_cache: Dict[str, HyperliquidCredentials] = {}
        self._http_client: Optional[httpx.AsyncClient] = None
        self._info_clients: Dict[str, "Info"] = {}  # Cached Info clients per user
        self._exchange_clients: Dict[str, "Exchange"] = {}  # Cached Exchange clients
        self._tools_cache: Optional[List[Dict[str, Any]]] = None

        self.logger = get_component_logger("HyperliquidTool")
        self._enabled = True

        if not _has_sdk():
            self.logger.warning("hyperliquid-python-sdk not installed - trading features disabled")

    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {
            'rate_limit_manager': 'Rate limit management for API calls'
        }

    @property
    def optional_services(self) -> Dict[str, str]:
        """Get optional services."""
        return {
            'cache_manager': 'Cache for market data',
            'database_manager': 'Database for credentials'
        }

    async def _initialize(self) -> None:
        """Initialize the tool"""
        await super()._initialize()

        # Create HTTP client with connection pooling
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=True
        )

        # Initialize database handler
        if self.container:
            self.db = self.container.get_service('hyperliquid_db')
            if not self.db:
                db_manager = self.container.get_service('database_manager')
                if db_manager:
                    from polyrob_markets.hyperliquid.store import HyperliquidDBHandler
                    self.db = HyperliquidDBHandler(db_manager.connection)
                    await self.db.ensure_tables()
                    self.container.register_service('hyperliquid_db', self.db)

        self._status = ToolStatus.HEALTHY
        self.logger.info("HyperliquidTool initialized")

    async def _cleanup(self) -> None:
        """Cleanup resources"""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._credentials_cache.clear()
        self._info_clients.clear()
        self._exchange_clients.clear()
        self._status = ToolStatus.UNINITIALIZED

        self.logger.info("HyperliquidTool cleaned up")

    # CR-M14: the tenant is TASK-local, never a mutable field on this shared
    # singleton. Each HTTP request and each agent turn runs in its own asyncio
    # task, so a sibling's ``set_user_context`` can no longer swap the user (and
    # with it the credentials and the signer) under an order that is between
    # two awaits. Tests and old callers that assign ``_user_id`` keep working.
    @property
    def _user_id(self) -> Optional[str]:
        return self._user_ctxvar().get()

    @_user_id.setter
    def _user_id(self, value: Optional[str]) -> None:
        self._user_ctxvar().set(value)

    def _user_ctxvar(self) -> "contextvars.ContextVar":
        var = self.__dict__.get("_user_ctx")
        if var is None:
            var = contextvars.ContextVar(
                f"{type(self).__name__}_user_{id(self)}", default=None)
            self.__dict__["_user_ctx"] = var
        return var

    def set_user_context(self, user_id: str) -> None:
        """Set the current user context for credential resolution"""
        if user_id != self._user_id:
            self._user_id = user_id
            self.logger.debug(f"Hyperliquid user context set: {user_id}")

    # =========================================================================
    # Credential Management
    # =========================================================================

    @staticmethod
    def _resolve_query_address(credentials: HyperliquidCredentials) -> str:
        """The address to query /info for. ALWAYS the master/owner account: an
        agent/API-wallet address returns an empty account for state/positions/fills."""
        return credentials.wallet_address

    async def _get_info_client(self) -> Optional["Info"]:
        """Get or create Info client for read-only operations"""
        if not _has_sdk():
            return None

        credentials = await self._get_user_credentials()
        api_url = credentials.api_url if credentials else MAINNET_API_URL

        cache_key = f"{self._user_id or 'anon'}:{api_url}"
        if cache_key in self._info_clients:
            return self._info_clients[cache_key]

        info = Info(api_url, skip_ws=True)
        self._info_clients[cache_key] = info
        return info

    async def _get_exchange_client(self) -> Tuple[Optional["Exchange"], Optional[str]]:
        """Get or create authenticated Exchange client for trading"""
        if not _has_sdk():
            return None, "hyperliquid-python-sdk not installed"

        credentials = await self._get_user_credentials()
        if not credentials:
            return None, "Credentials not configured"

        if not credentials.is_configured():
            return None, "Wallet not properly configured"

        if credentials.demo_mode:
            return None, "Trading disabled in demo mode"

        cache_key = f"{self._user_id}:{credentials.api_url}"
        if cache_key in self._exchange_clients:
            return self._exchange_clients[cache_key], None

        # Get Info client first (required by Exchange)
        info = await self._get_info_client()
        if not info:
            return None, "Failed to create info client"

        try:
            # Prefer the agent personal wallet (gated) over per-user DB creds.
            from core.wallet.factory import get_agent_wallet
            agent_wallet = get_agent_wallet()
            if agent_wallet is not None:
                # H4 (2026-08-22 audit, still open): on this branch the derived
                # venue key fully OWNS the account it trades — the approveAgent
                # master/agent split that gives Hyperliquid its withdrawal
                # firewall is never constructed, so the ordering key could also
                # withdraw. Refuse to hand out an exchange client while live
                # trading is armed on this un-firewalled signer; dry-run and
                # reads are unaffected, and the DB-credential delegated path
                # below is not restricted.
                from core.env import bool_env as _h4_bool_env
                if _h4_bool_env("CRYPTO_TRADE_LIVE_ENABLED", False) and \
                        _h4_bool_env("HYPERLIQUID_TRADING_ENABLED", False):
                    return None, (
                        "refused: live Hyperliquid trading is armed, but the "
                        "polyrob wallet path signs with a key that fully owns "
                        "its own account — the approveAgent withdrawal firewall "
                        "(H4) is not in place. Keep HYPERLIQUID_TRADING_ENABLED "
                        "off on this path, or configure delegated credentials "
                        "(approve_agent) instead."
                    )
                account = agent_wallet.account_for("hyperliquid")
                # 066 P3: under WALLET_SIGNER=remote `account` is a RemoteAccount;
                # route the SDK's L1 (order) signing to polyrob-signer.
                from polyrob_markets.hyperliquid.remote_signing import install as _install_remote_hl
                _install_remote_hl()
                # SDK signature: Exchange(wallet: LocalAccount, base_url=None, ...,
                # account_address=None). The agent key IS the signer here; the
                # master/account distinction (approveAgent) is the deferred
                # funding-ops follow-on.
                exchange = Exchange(
                    account,
                    base_url=credentials.api_url,
                    account_address=account.address,
                )
            else:
                # P0-2 fix: the SDK `wallet` arg is a LocalAccount SIGNER, not an
                # address string. Build the account from the stored key and use
                # the keyword signature, mirroring the agent-wallet branch.
                from eth_account import Account
                try:
                    account = Account.from_key(credentials.trading_private_key)
                except Exception:
                    # Never echo the raw key in the surfaced error/log.
                    return None, "invalid trading private key"
                # When a delegated agent_wallet signs, the SDK must trade for the
                # MASTER account (credentials.wallet_address), NOT the agent address —
                # querying/trading the agent address hits an empty account. With no
                # agent_wallet the signer IS the master, so wallet_address is still right.
                exchange = Exchange(
                    account,
                    base_url=credentials.api_url,
                    account_address=credentials.wallet_address or account.address,
                )
            self._exchange_clients[cache_key] = exchange
            return exchange, None
        except Exception as e:
            self.logger.error(f"Failed to create exchange client: {e}")
            return None, str(e)

    # =========================================================================
    # Trading Limits Check
    # =========================================================================

    async def _check_trading_limits(
        self,
        credentials: HyperliquidCredentials,
        coin: str,
        size_usd: float,
        leverage: int = 1
    ) -> Tuple[bool, str]:
        """Validate order against trading limits"""
        limits = credentials.trading_limits
        from polyrob_markets.risk_limits import validate_limits
        try:
            validate_limits(limits)
        except (TypeError, ValueError):
            return False, "Invalid trading limits; refusing to trade"

        # Check if trading is enabled
        if not credentials.can_trade():
            return False, "Trading not enabled. Set demo_mode=False and enable_autonomous_trading=True"

        # Check coin restrictions
        if limits.blocked_coins and coin.upper() in [c.upper() for c in limits.blocked_coins]:
            return False, f"Coin '{coin}' is blocked"

        if limits.allowed_coins and coin.upper() not in [c.upper() for c in limits.allowed_coins]:
            return False, f"Coin '{coin}' not in allowed list"

        # Check order size
        if size_usd > limits.max_order_size_usd:
            return False, f"Order size ${size_usd:.2f} exceeds max ${limits.max_order_size_usd:.2f}"

        # Check leverage
        if leverage > limits.max_leverage:
            return False, f"Leverage {leverage}x exceeds max {limits.max_leverage}x"

        return True, "OK"

    async def _check_exposure(
        self, credentials, new_value_usd: float, reduce_only: bool, coin: str = ""
    ) -> Tuple[bool, str]:
        """Check exposure, liquidity, spread and live leverage under the reserve lock."""
        from polyrob_markets.risk_limits import hyperliquid_limits, RiskLimitError
        try:
            await hyperliquid_limits(self, credentials, coin.upper(), new_value_usd, reduce_only)
        except RiskLimitError as exc:
            return False, f"{exc}; refusing to open"
        except Exception:
            return False, "Cannot verify trading limits from complete live data; refusing to open"
        return True, "OK"

    async def _check_daily_loss(self, credentials, reduce_only: bool) -> Tuple[bool, str]:
        """Enforce the daily-loss stop from complete, finite fills and positions."""
        from polyrob_markets.risk_limits import hyperliquid_daily_loss, RiskLimitError
        try:
            await hyperliquid_daily_loss(self, credentials, reduce_only)
        except RiskLimitError as exc:
            return False, f"{exc}; refusing to open"
        except Exception:
            return False, "Cannot verify daily loss; refusing to open"
        return True, "OK"

    # =========================================================================
    # Market Discovery Actions
    # =========================================================================

    @BaseTool.action(
        'Get all available perpetual futures markets with metadata',
        param_model=EmptyParams
    )
    async def get_perpetual_markets(self, params: EmptyParams) -> Dict[str, Any]:
        """Get all available perpetual markets"""
        await self.ensure_initialized()
        await self.rate_limit("get_perpetual_markets")

        start_time = time.time()
        try:
            credentials = await self._get_user_credentials()
            api_url = credentials.api_url if credentials else MAINNET_API_URL

            response = await self._http_client.post(
                f"{api_url}/info",
                json={"type": "metaAndAssetCtxs"}
            )
            response.raise_for_status()
            data = response.json()

            # Parse universe and asset contexts
            markets = []
            if len(data) >= 2:
                meta = data[0]
                asset_ctxs = data[1]

                for i, asset in enumerate(meta.get("universe", [])):
                    ctx = asset_ctxs[i] if i < len(asset_ctxs) else {}
                    markets.append({
                        "coin": asset.get("name"),
                        "max_leverage": asset.get("maxLeverage"),
                        "mark_price": ctx.get("markPx"),
                        "funding_rate": ctx.get("funding"),
                        "open_interest": ctx.get("openInterest"),
                        "volume_24h": ctx.get("dayNtlVlm"),
                    })

            return {
                "success": True,
                "markets": markets,
                "count": len(markets),
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"get_perpetual_markets failed: {e}")
            return {"success": False, "error": str(e), "markets": []}

    @BaseTool.action(
        'Get all available spot trading pairs',
        param_model=EmptyParams
    )
    async def get_spot_markets(self, params: EmptyParams) -> Dict[str, Any]:
        """Get all available spot markets"""
        await self.ensure_initialized()
        await self.rate_limit("get_spot_markets")

        start_time = time.time()
        try:
            credentials = await self._get_user_credentials()
            api_url = credentials.api_url if credentials else MAINNET_API_URL

            response = await self._http_client.post(
                f"{api_url}/info",
                json={"type": "spotMetaAndAssetCtxs"}
            )
            response.raise_for_status()
            data = response.json()

            markets = []
            if len(data) >= 2:
                meta = data[0]
                asset_ctxs = data[1]

                tokens = {t["index"]: t for t in meta.get("tokens", [])}
                for i, pair in enumerate(meta.get("universe", [])):
                    ctx = asset_ctxs[i] if i < len(asset_ctxs) else {}
                    pair_tokens = pair.get("tokens", [0, 0])
                    base_token = tokens.get(pair_tokens[0], {}) if len(pair_tokens) > 0 else {}
                    quote_token = tokens.get(pair_tokens[1], {}) if len(pair_tokens) > 1 else {}

                    markets.append({
                        "pair": pair.get("name"),
                        "base": base_token.get("name"),
                        "quote": quote_token.get("name"),
                        "index": pair.get("index"),
                        "mark_price": ctx.get("markPx"),
                        "mid_price": ctx.get("midPx"),
                        "volume_24h": ctx.get("dayNtlVlm"),
                    })

            return {
                "success": True,
                "markets": markets,
                "count": len(markets),
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"get_spot_markets failed: {e}")
            return {"success": False, "error": str(e), "markets": []}

    # =========================================================================
    # Market Data Actions
    # =========================================================================

    @BaseTool.action(
        'Get current mid price for a coin',
        param_model=GetPriceParams
    )
    async def get_current_price(self, params: GetPriceParams) -> Dict[str, Any]:
        """Get current price for a coin"""
        await self.ensure_initialized()
        await self.rate_limit("get_current_price")

        start_time = time.time()
        try:
            credentials = await self._get_user_credentials()
            api_url = credentials.api_url if credentials else MAINNET_API_URL

            response = await self._http_client.post(
                f"{api_url}/info",
                json={"type": "allMids"}
            )
            response.raise_for_status()
            mids = response.json()

            coin = params.coin.upper()
            if coin not in mids:
                return {
                    "success": False,
                    "error": f"Coin '{coin}' not found",
                    "available_coins": list(mids.keys())[:20]
                }

            return {
                "success": True,
                "coin": coin,
                "mid_price": float(mids[coin]),
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"get_current_price failed: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Get all mid prices for all markets',
        param_model=EmptyParams
    )
    async def get_all_mids(self, params: EmptyParams) -> Dict[str, Any]:
        """Get all mid prices"""
        await self.ensure_initialized()
        await self.rate_limit("get_all_mids")

        start_time = time.time()
        try:
            credentials = await self._get_user_credentials()
            api_url = credentials.api_url if credentials else MAINNET_API_URL

            response = await self._http_client.post(
                f"{api_url}/info",
                json={"type": "allMids"}
            )
            response.raise_for_status()
            mids = response.json()

            return {
                "success": True,
                "prices": {k: float(v) for k, v in mids.items()},
                "count": len(mids),
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"get_all_mids failed: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Get L2 orderbook with bid/ask levels',
        param_model=GetOrderbookParams
    )
    async def get_orderbook(self, params: GetOrderbookParams) -> Dict[str, Any]:
        """Get L2 orderbook for a coin"""
        await self.ensure_initialized()
        await self.rate_limit("get_orderbook")

        start_time = time.time()
        try:
            credentials = await self._get_user_credentials()
            api_url = credentials.api_url if credentials else MAINNET_API_URL

            response = await self._http_client.post(
                f"{api_url}/info",
                json={"type": "l2Book", "coin": params.coin.upper()}
            )
            response.raise_for_status()
            data = response.json()

            levels = data.get("levels", [[], []])
            bids = levels[0][:params.depth] if len(levels) > 0 else []
            asks = levels[1][:params.depth] if len(levels) > 1 else []

            # Calculate spread
            best_bid = float(bids[0]["px"]) if bids else 0
            best_ask = float(asks[0]["px"]) if asks else 0
            spread = (best_ask - best_bid) / best_bid if best_bid > 0 else 0

            return {
                "success": True,
                "coin": params.coin.upper(),
                "bids": [{"price": float(b["px"]), "size": float(b["sz"])} for b in bids],
                "asks": [{"price": float(a["px"]), "size": float(a["sz"])} for a in asks],
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "spread_bps": spread * 10000,
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"get_orderbook failed: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Get current funding rate for a perpetual',
        param_model=GetFundingParams
    )
    async def get_funding_rate(self, params: GetFundingParams) -> Dict[str, Any]:
        """Get current and predicted funding rate for a coin"""
        await self.ensure_initialized()
        await self.rate_limit("get_funding_rate")

        start_time = time.time()
        try:
            credentials = await self._get_user_credentials()
            api_url = credentials.api_url if credentials else MAINNET_API_URL

            response = await self._http_client.post(
                f"{api_url}/info",
                json={"type": "metaAndAssetCtxs"}
            )
            response.raise_for_status()
            data = response.json()

            coin = params.coin.upper()
            funding_info = None

            if len(data) >= 2:
                meta = data[0]
                asset_ctxs = data[1]

                for i, asset in enumerate(meta.get("universe", [])):
                    if asset.get("name") == coin:
                        ctx = asset_ctxs[i] if i < len(asset_ctxs) else {}
                        funding_info = {
                            "coin": coin,
                            "current_funding": ctx.get("funding"),
                            "mark_price": ctx.get("markPx"),
                            "premium": ctx.get("premium"),
                            "open_interest": ctx.get("openInterest"),
                        }
                        break

            if not funding_info:
                return {"success": False, "error": f"Coin '{coin}' not found"}

            return {
                "success": True,
                **funding_info,
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"get_funding_rate failed: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # Portfolio Actions
    # =========================================================================

    @BaseTool.action(
        'Get perpetuals account summary including margin and positions',
        param_model=EmptyParams
    )
    async def get_account_state(self, params: EmptyParams) -> Dict[str, Any]:
        """Get perpetuals account summary"""
        await self.ensure_initialized()
        await self.rate_limit("get_account_state")

        start_time = time.time()
        try:
            credentials = await self._get_user_credentials()
            if not credentials:
                return {"success": False, "error": "Credentials not configured"}

            response = await self._http_client.post(
                f"{credentials.api_url}/info",
                json={
                    "type": "clearinghouseState",
                    "user": self._resolve_query_address(credentials)
                }
            )
            response.raise_for_status()
            data = response.json()

            # Parse margin summary
            margin = data.get("marginSummary", {})
            positions = data["assetPositions"]

            return {
                "success": True,
                "wallet_address": credentials.wallet_address,
                "account_value": float(margin.get("accountValue", 0)),
                "total_margin_used": float(margin.get("totalMarginUsed", 0)),
                "total_ntl_pos": float(margin["totalNtlPos"]),
                "total_raw_usd": float(margin.get("totalRawUsd", 0)),
                "withdrawable": float(data.get("withdrawable", 0)),
                "cross_margin_summary": data.get("crossMarginSummary"),
                "position_count": len(positions),
                "positions": [
                    {
                        "coin": p.get("position", {}).get("coin"),
                        "size": float(p.get("position", {}).get("szi", 0)),
                        "entry_price": float(p.get("position", {}).get("entryPx", 0)),
                        "unrealized_pnl": float(p.get("position", {}).get("unrealizedPnl", 0)),
                        "leverage": p.get("position", {}).get("leverage"),
                        "position_value": float(p["position"]["positionValue"]),
                        "liquidation_px": p.get("position", {}).get("liquidationPx"),
                        "margin_used": float(p.get("position", {}).get("marginUsed", 0)),
                    }
                    for p in positions
                ],
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"get_account_state failed: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Get spot token balances',
        param_model=EmptyParams
    )
    async def get_spot_balances(self, params: EmptyParams) -> Dict[str, Any]:
        """Get spot token balances"""
        await self.ensure_initialized()
        await self.rate_limit("get_spot_balances")

        start_time = time.time()
        try:
            credentials = await self._get_user_credentials()
            if not credentials:
                return {"success": False, "error": "Credentials not configured"}

            response = await self._http_client.post(
                f"{credentials.api_url}/info",
                json={
                    "type": "spotClearinghouseState",
                    "user": self._resolve_query_address(credentials)
                }
            )
            response.raise_for_status()
            data = response.json()

            balances = []
            for balance in data.get("balances", []):
                total = float(balance.get("total", 0))
                hold = float(balance.get("hold", 0))
                balances.append({
                    "coin": balance.get("coin"),
                    "total": total,
                    "hold": hold,
                    "available": total - hold,
                    "entry_notional": float(balance.get("entryNtl", 0)),
                })

            return {
                "success": True,
                "wallet_address": credentials.wallet_address,
                "balances": balances,
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"get_spot_balances failed: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Get all open orders',
        param_model=EmptyParams
    )
    async def get_open_orders(self, params: EmptyParams) -> Dict[str, Any]:
        """Get all open orders"""
        await self.ensure_initialized()
        await self.rate_limit("get_open_orders")

        start_time = time.time()
        try:
            credentials = await self._get_user_credentials()
            if not credentials:
                return {"success": False, "error": "Credentials not configured"}

            response = await self._http_client.post(
                f"{credentials.api_url}/info",
                json={
                    "type": "openOrders",
                    "user": self._resolve_query_address(credentials)
                }
            )
            response.raise_for_status()
            orders = response.json()

            return {
                "success": True,
                "orders": [
                    {
                        "order_id": o.get("oid"),
                        "coin": o.get("coin"),
                        "side": "buy" if o.get("side") == "B" else "sell",
                        "size": float(o["sz"]),
                        "price": float(o["limitPx"]),
                        "filled": float(o.get("origSz", 0)) - float(o.get("sz", 0)),
                        "order_type": o.get("orderType"),
                        "reduce_only": o.get("reduceOnly", False),
                        "timestamp": o.get("timestamp"),
                    }
                    for o in orders
                ],
                "count": len(orders),
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"get_open_orders failed: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Get user fill history',
        param_model=GetFillsParams
    )
    async def get_fills(self, params: GetFillsParams) -> Dict[str, Any]:
        """Get fill history"""
        await self.ensure_initialized()
        await self.rate_limit("get_fills")

        start_time = time.time()
        try:
            credentials = await self._get_user_credentials()
            if not credentials:
                return {"success": False, "error": "Credentials not configured"}

            response = await self._http_client.post(
                f"{credentials.api_url}/info",
                json={
                    "type": "userFills",
                    "user": self._resolve_query_address(credentials)
                }
            )
            response.raise_for_status()
            fills = response.json()

            # Limit results
            fills = sorted(fills, key=lambda f: int(f["time"]), reverse=True)[:params.limit]

            return {
                "success": True,
                "fills": [
                    {
                        "coin": f.get("coin"),
                        "side": "buy" if f.get("side") == "B" else "sell",
                        "price": float(f.get("px", 0)),
                        "size": float(f.get("sz", 0)),
                        "time": f.get("time"),
                        "fee": float(f["fee"]),
                        "fee_token": f.get("feeToken"),
                        "start_position": f.get("startPosition"),
                        "closed_pnl": float(f["closedPnl"]),
                    }
                    for f in fills
                ],
                "count": len(fills),
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"get_fills failed: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # Trading Actions
    # =========================================================================

    @BaseTool.action(
        'Place a limit order for perpetuals or spot',
        param_model=PlaceLimitOrderParams
    )
    async def place_limit_order(self, params: PlaceLimitOrderParams, execution_context=None) -> Dict[str, Any]:
        """Place a limit order"""
        await self.ensure_initialized()

        # H11: a forged/autonomous turn (self-wake, delegation-result, leaf, autonomous
        # run) OR the owner kill-switch (autonomy_halted) blocks a live order — the owner
        # must drive trades. Parity with x402 spend + the owner-queue approver.
        from polyrob_markets.trade_gate import trade_turn_refusal
        refusal = trade_turn_refusal(execution_context, self,
                                     entry=not params.reduce_only)
        if refusal:
            return {"success": False, "error": refusal, "forged_turn_blocked": True}

        start_time = time.time()

        credentials = await self._get_user_credentials()
        if not credentials:
            return {"success": False, "error": "Credentials not configured"}

        # A model-chosen sell price must not understate the assets at risk.
        from polyrob_markets.order_safety import order_notional
        price_res = await self.get_current_price(GetPriceParams(coin=params.coin))
        try:
            if not price_res.get("success"):
                raise ValueError("Cannot verify live order price")
            order_value_usd = order_notional(params.size, params.price, price_res.get("mid_price"))
        except (ValueError, TypeError):
            return {"success": False, "error": "Cannot value order within the live reference price band"}
        if order_value_usd > params.max_usd:
            return {"success": False, "error": "Live order notional exceeds approved max_usd"}

        # Check trading limits
        can_trade, message = await self._check_trading_limits(
            credentials, params.coin, order_value_usd
        )
        if not can_trade:
            return {"success": False, "error": message}

        # Check if confirmation required
        if order_value_usd > credentials.trading_limits.require_confirmation_above_usd:
            return {
                "success": False,
                "error": f"Orders above ${credentials.trading_limits.require_confirmation_above_usd} require manual confirmation",
                "order_details": {
                    "coin": params.coin,
                    "side": "buy" if params.is_buy else "sell",
                    "price": params.price,
                    "size": params.size,
                    "value_usd": order_value_usd,
                }
            }

        from core.wallet.factory import get_policy_gate
        policy = get_policy_gate()
        # M4 (2026-07-15): hold the gate's reserve lock across check -> submit -> record
        # so two concurrent orders can't both pass a nearly-exhausted cap at the same
        # stale rolling-spend and then both record (clearing past the cap). Mirrors
        # x402/service.py::x402_fetch. The dry-run early-returns inside the block exit the
        # lock without recording (no spend happened); evaluate_live_trade / the SDK submit
        # never re-enter policy.check/record, so there is no self-deadlock.
        async with policy.reserve():
            decision = policy.check(venue="hyperliquid", amount_usd=_with_fee(order_value_usd), idempotency_key=None)
            if not decision.allowed:
                return {"success": False, "error": f"Policy gate denied: {decision.reason}"}

            # T11 live kill-switch: dry-run unless master + venue switches on AND within cap.
            from polyrob_markets.trade_gate import evaluate_live_trade
            gate = evaluate_live_trade("hyperliquid", order_value_usd)
            if not gate.live:
                return {"success": False, "dry_run": True,
                        "error": f"Order not submitted (dry-run): {gate.reason}",
                        "order_details": {"coin": params.coin, "is_buy": params.is_buy,
                                          "size": params.size, "price": params.price}}

            ok_exp, exp_msg = await self._check_exposure(
                credentials, order_value_usd, params.reduce_only, params.coin)
            if not ok_exp:
                return {"success": False, "error": exp_msg}
            ok_dl, dl_msg = await self._check_daily_loss(credentials, params.reduce_only)
            if not ok_dl:
                return {"success": False, "error": dl_msg}

            await self.rate_limit("place_limit_order")

            exchange, error = await self._get_exchange_client()
            if error:
                return {"success": False, "error": error}

            try:
                # Determine order type
                order_type = {"limit": {"tif": "Alo" if params.post_only else "Gtc"}}

                from polyrob_markets.order_submission import submit_order
                result, submission_ref = await asyncio.to_thread(
                    submit_order, "hyperliquid", exchange, self._user_id, _with_fee(order_value_usd),
                    name=params.coin.upper(),
                    is_buy=params.is_buy,
                    sz=params.size,
                    limit_px=params.price,
                    order_type=order_type,
                    reduce_only=params.reduce_only,
                    cloid=params.client_order_id,
                )

                from polyrob_markets.order_safety import order_accepted, order_refusal
                if not order_accepted("hyperliquid", result):
                    return {"success": False, "error": order_refusal("hyperliquid", result, submission_ref)}

                policy.record(
                    venue="hyperliquid", action="place_limit_order",
                    amount_usd=_with_fee(order_value_usd), counterparty=params.coin.upper(),
                    idempotency_key=None, result_ref=str(result)[:80],
                    chain="hyperliquid", submission_ref=submission_ref,
                )

                # Audit log
                if self.db:
                    await self.db.audit_log(
                        user_id=self._user_id,
                        action="place_limit_order",
                        tool_name="place_limit_order",
                        market_id=params.coin,
                        details={
                            "side": "buy" if params.is_buy else "sell",
                            "size": params.size,
                            "price": params.price,
                            "result": result,
                        }
                    )

                return {
                    "success": True,
                    "result": result,
                    "order_details": {
                        "coin": params.coin.upper(),
                        "side": "buy" if params.is_buy else "sell",
                        "size": params.size,
                        "price": params.price,
                        "value_usd": order_value_usd,
                    },
                    "execution_time_ms": (time.time() - start_time) * 1000
                }
            except Exception as e:
                self.logger.error(f"place_limit_order failed: {e}")
                return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Place a market order (marketable IOC at mid +/- slippage) for perpetuals or spot',
        param_model=PlaceMarketOrderParams
    )
    async def place_market_order(self, params: PlaceMarketOrderParams, execution_context=None) -> Dict[str, Any]:
        """Place a market order as a marketable IOC limit at mid +/- slippage."""
        await self.ensure_initialized()

        # H11: forged/autonomous turns OR the owner kill-switch block a live order
        # (see place_limit_order).
        from polyrob_markets.trade_gate import trade_turn_refusal
        refusal = trade_turn_refusal(execution_context, self,
                                     entry=not params.reduce_only)
        if refusal:
            return {"success": False, "error": refusal, "forged_turn_blocked": True}

        start_time = time.time()

        credentials = await self._get_user_credentials()
        if not credentials:
            return {"success": False, "error": "Credentials not configured"}

        # A market order needs a live mid to both size (USD) and price the
        # marketable limit. Never submit a naked market order without a price.
        price_res = await self.get_current_price(GetPriceParams(coin=params.coin))
        if not price_res.get("success"):
            return {
                "success": False,
                "error": f"No live price for {params.coin.upper()}: {price_res.get('error')}",
            }
        from polyrob_markets.order_safety import order_notional
        try:
            mid = float(price_res["mid_price"])
            limit_px = mid * (1 + params.slippage) if params.is_buy else mid * (1 - params.slippage)
            order_value_usd = order_notional(params.size, limit_px, mid)
        except (KeyError, TypeError, ValueError):
            return {"success": False, "error": "Cannot verify finite live order notional"}
        if order_value_usd > params.max_usd:
            return {"success": False, "error": "Live order notional exceeds approved max_usd"}

        # Check trading limits
        can_trade, message = await self._check_trading_limits(
            credentials, params.coin, order_value_usd
        )
        if not can_trade:
            return {"success": False, "error": message}

        # Check if confirmation required
        if order_value_usd > credentials.trading_limits.require_confirmation_above_usd:
            return {
                "success": False,
                "error": f"Orders above ${credentials.trading_limits.require_confirmation_above_usd} require manual confirmation",
                "order_details": {
                    "coin": params.coin.upper(),
                    "side": "buy" if params.is_buy else "sell",
                    "size": params.size,
                    "mid_price": mid,
                    "value_usd": order_value_usd,
                },
            }

        from core.wallet.factory import get_policy_gate
        policy = get_policy_gate()
        # M4 (2026-07-15): serialize check -> submit -> record under the gate's reserve
        # lock (see place_limit_order for the rationale / no-deadlock note).
        async with policy.reserve():
            decision = policy.check(venue="hyperliquid", amount_usd=_with_fee(order_value_usd), idempotency_key=None)
            if not decision.allowed:
                return {"success": False, "error": f"Policy gate denied: {decision.reason}"}

            # T11 live kill-switch: dry-run unless master + venue switches on AND within cap.
            from polyrob_markets.trade_gate import evaluate_live_trade
            gate = evaluate_live_trade("hyperliquid", order_value_usd)
            if not gate.live:
                return {"success": False, "dry_run": True,
                        "error": f"Order not submitted (dry-run): {gate.reason}",
                        "order_details": {"coin": params.coin, "is_buy": params.is_buy,
                                          "size": params.size}}

            ok_exp, exp_msg = await self._check_exposure(
                credentials, order_value_usd, params.reduce_only, params.coin)
            if not ok_exp:
                return {"success": False, "error": exp_msg}
            ok_dl, dl_msg = await self._check_daily_loss(credentials, params.reduce_only)
            if not ok_dl:
                return {"success": False, "error": dl_msg}

            await self.rate_limit("place_market_order")

            exchange, error = await self._get_exchange_client()
            if error:
                return {"success": False, "error": error}

            try:
                order_type = {"limit": {"tif": "Ioc"}}
                from polyrob_markets.order_submission import submit_order
                result, submission_ref = await asyncio.to_thread(
                    submit_order, "hyperliquid", exchange, self._user_id, _with_fee(order_value_usd),
                    name=params.coin.upper(),
                    is_buy=params.is_buy,
                    sz=params.size,
                    limit_px=limit_px,
                    order_type=order_type,
                    reduce_only=params.reduce_only,
                )

                from polyrob_markets.order_safety import order_accepted, order_refusal
                if not order_accepted("hyperliquid", result):
                    return {"success": False, "error": order_refusal("hyperliquid", result, submission_ref)}

                policy.record(
                    venue="hyperliquid", action="place_market_order",
                    amount_usd=_with_fee(order_value_usd), counterparty=params.coin.upper(),
                    idempotency_key=None, result_ref=str(result)[:80],
                    chain="hyperliquid", submission_ref=submission_ref,
                )

                if self.db:
                    await self.db.audit_log(
                        user_id=self._user_id,
                        action="place_market_order",
                        tool_name="place_market_order",
                        market_id=params.coin,
                        details={
                            "side": "buy" if params.is_buy else "sell",
                            "size": params.size,
                            "mid_price": mid,
                            "limit_px": limit_px,
                            "slippage": params.slippage,
                            "result": result,
                        },
                    )

                return {
                    "success": True,
                    "result": result,
                    "order_details": {
                        "coin": params.coin.upper(),
                        "side": "buy" if params.is_buy else "sell",
                        "size": params.size,
                        "mid_price": mid,
                        "limit_px": limit_px,
                        "slippage": params.slippage,
                        "value_usd": order_value_usd,
                    },
                    "execution_time_ms": (time.time() - start_time) * 1000,
                }
            except Exception as e:
                self.logger.error(f"place_market_order failed: {e}")
                return {"success": False, "error": str(e)}

    # A cancel is risk-reducing EXCEPT for a trigger order: a stop-loss or a
    # take-profit IS the protection, so cancelling one can leave a position
    # unprotected. Those keep the owner tap (asked here, inside the verb); every
    # other cancel retires a resting order and runs without one.
    async def _trigger_orders_in_scope(self, coin: Optional[str],
                                       order_id: Optional[int] = None) -> Optional[List[Dict[str, Any]]]:
        """The TRIGGER orders this cancel would remove, or None when the venue
        cannot be read (the caller then treats the cancel as a trigger cancel)."""
        try:
            credentials = await self._get_user_credentials()
            if not credentials:
                return None
            response = await self._http_client.post(
                f"{credentials.api_url}/info",
                json={"type": "frontendOpenOrders",
                      "user": self._resolve_query_address(credentials)})
            response.raise_for_status()
            orders = response.json()
            if not isinstance(orders, list):
                return None
        except Exception:
            self.logger.debug("hyperliquid: open-order read for a cancel failed", exc_info=True)
            return None
        want_coin = coin.upper() if coin else None
        hits = []
        for o in orders:
            if not isinstance(o, dict):
                return None
            if want_coin and str(o.get("coin") or "").upper() != want_coin:
                continue
            if order_id is not None and o.get("oid") != order_id:
                continue
            kind = str(o.get("orderType") or "")
            if o.get("isTrigger") or o.get("isPositionTpsl") or \
                    any(w in kind for w in ("Stop", "Take Profit", "Trigger")):
                hits.append(o)
        return hits

    async def _trigger_cancel_refusal(self, action: str, scope: Dict[str, Any],
                                      order_id: Optional[int],
                                      execution_context) -> Optional[str]:
        """None when the cancel may run; else why it did not. A direct/CLI call
        (no execution context) is the owner acting and is never asked."""
        if execution_context is None:
            return None
        triggers = await self._trigger_orders_in_scope(scope.get("coin"), order_id)
        if triggers == []:
            return None
        summary = dict(scope)
        summary["why"] = ("removes a stop-loss / take-profit order" if triggers
                          else "the open orders could not be read, so it may remove a stop-loss")
        try:
            from tools.controller.approval import approval_wait_timeout_sec
            from tools.controller.approval_queue import OwnerQueueApprover
            approver = OwnerQueueApprover(user_id=getattr(execution_context, "user_id", None))
            try:
                ok = await asyncio.wait_for(
                    approver.request(action, summary, execution_context, hash_params=scope),
                    timeout=approval_wait_timeout_sec("owner_queue"))
            except asyncio.TimeoutError:
                ok = False
        except Exception as exc:
            self.logger.warning("hyperliquid: trigger-cancel approval failed", exc_info=True)
            return f"approval could not be obtained ({exc}); failing closed — nothing was cancelled"
        if ok:
            return None
        return ("this cancel would remove a stop-loss / take-profit order, so it needs the "
                "owner's approval. The ask is waiting (it shows in /pending); once granted, "
                "the same cancel runs. Nothing was cancelled.")

    @BaseTool.action(
        'Cancel an open order',
        param_model=CancelOrderParams
    )
    async def cancel_order(self, params: CancelOrderParams, execution_context=None) -> Dict[str, Any]:
        """Cancel an order"""
        await self.ensure_initialized()

        # H11: a forged/autonomous turn OR the owner kill-switch can never mutate the
        # owner's live orders (parity with place_*_order).
        from polyrob_markets.trade_gate import trade_turn_refusal
        refusal = trade_turn_refusal(execution_context, self, risk_reducing=True)
        if refusal:
            return {"success": False, "error": refusal, "forged_turn_blocked": True}

        # M10/R16: a cancel is a position-management action the OWNER may still
        # perform BY HAND while halted — trade_turn_refusal's kill-switch bar lifts
        # ONLY for a direct/CLI call (execution_context is None above), never for an
        # agent-loop turn (even a genuine one). Still requires the live master +
        # per-venue switches, same as an order.
        from polyrob_markets.trade_gate import evaluate_live_mutation
        gate = evaluate_live_mutation("hyperliquid", risk_reducing=True)
        if not gate.live:
            return {"success": False, "error": gate.reason}

        trigger_refusal = await self._trigger_cancel_refusal(
            "hyperliquid_cancel_order",
            {"coin": params.coin.upper(), "order_id": params.order_id},
            params.order_id, execution_context)
        if trigger_refusal:
            return {"success": False, "error": trigger_refusal, "owner_approval_pending": True}

        await self.rate_limit("cancel_order")

        start_time = time.time()

        credentials = await self._get_user_credentials()
        if not credentials or not credentials.can_trade():
            return {"success": False, "error": "Trading not enabled"}

        exchange, error = await self._get_exchange_client()
        if error:
            return {"success": False, "error": error}

        try:
            result = exchange.cancel(params.coin.upper(), params.order_id)

            # Audit log
            if self.db:
                await self.db.audit_log(
                    user_id=self._user_id,
                    action="cancel_order",
                    tool_name="cancel_order",
                    market_id=params.coin,
                    details={"order_id": params.order_id, "result": result}
                )

            return {
                "success": True,
                "result": result,
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"cancel_order failed: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Cancel all open orders',
        param_model=CancelAllOrdersParams
    )
    async def cancel_all_orders(self, params: CancelAllOrdersParams, execution_context=None) -> Dict[str, Any]:
        """Cancel all open orders"""
        await self.ensure_initialized()

        # H11: forged/autonomous turns OR the owner kill-switch cannot mutate live orders.
        from polyrob_markets.trade_gate import trade_turn_refusal
        refusal = trade_turn_refusal(execution_context, self, risk_reducing=True)
        if refusal:
            return {"success": False, "error": refusal, "forged_turn_blocked": True}

        # M10/R16: OWNER-only carve-out — permitted while halted ONLY for a
        # direct/CLI call (execution_context is None above); any agent-loop turn is
        # still refused by trade_turn_refusal's kill-switch bar. Still needs the
        # live master + venue switches.
        from polyrob_markets.trade_gate import evaluate_live_mutation
        gate = evaluate_live_mutation("hyperliquid", risk_reducing=True)
        if not gate.live:
            return {"success": False, "error": gate.reason}

        trigger_refusal = await self._trigger_cancel_refusal(
            "hyperliquid_cancel_all_orders",
            {"coin": params.coin.upper() if params.coin else None}, None,
            execution_context)
        if trigger_refusal:
            return {"success": False, "error": trigger_refusal, "owner_approval_pending": True}

        await self.rate_limit("cancel_all_orders")

        start_time = time.time()

        credentials = await self._get_user_credentials()
        if not credentials or not credentials.can_trade():
            return {"success": False, "error": "Trading not enabled"}

        exchange, error = await self._get_exchange_client()
        if error:
            return {"success": False, "error": error}

        try:
            if params.coin:
                result = exchange.cancel_all_orders(params.coin.upper())
            else:
                result = exchange.cancel_all_orders()

            # Audit log
            if self.db:
                await self.db.audit_log(
                    user_id=self._user_id,
                    action="cancel_all_orders",
                    tool_name="cancel_all_orders",
                    market_id=params.coin,
                    details={"coin": params.coin, "result": result}
                )

            return {
                "success": True,
                "result": result,
                "coin": params.coin,
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"cancel_all_orders failed: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Update leverage for a perpetual market',
        param_model=UpdateLeverageParams
    )
    async def update_leverage(self, params: UpdateLeverageParams, execution_context=None) -> Dict[str, Any]:
        """Update leverage for a coin"""
        await self.ensure_initialized()

        # H11: forged/autonomous turns OR the owner kill-switch cannot change leverage
        # (a risk-posture mutation) — the owner must drive it.
        from polyrob_markets.trade_gate import trade_turn_refusal
        refusal = trade_turn_refusal(execution_context, self)
        if refusal:
            return {"success": False, "error": refusal, "forged_turn_blocked": True}

        # M10: a leverage change is NOT risk-reducing (raising leverage raises
        # liquidation risk) — blocked like an order while autonomy is halted.
        from polyrob_markets.trade_gate import evaluate_live_mutation
        gate = evaluate_live_mutation("hyperliquid", risk_reducing=False)
        if not gate.live:
            return {"success": False, "error": gate.reason}

        await self.rate_limit("update_leverage")

        start_time = time.time()

        credentials = await self._get_user_credentials()
        if not credentials or not credentials.can_trade():
            return {"success": False, "error": "Trading not enabled"}

        # Check against max allowed leverage
        if params.leverage > credentials.trading_limits.max_leverage:
            return {
                "success": False,
                "error": f"Leverage {params.leverage}x exceeds max allowed {credentials.trading_limits.max_leverage}x"
            }

        exchange, error = await self._get_exchange_client()
        if error:
            return {"success": False, "error": error}

        try:
            result = exchange.update_leverage(
                params.leverage,
                params.coin.upper(),
                params.is_cross
            )

            # Audit log
            if self.db:
                await self.db.audit_log(
                    user_id=self._user_id,
                    action="update_leverage",
                    tool_name="update_leverage",
                    market_id=params.coin,
                    details={
                        "leverage": params.leverage,
                        "is_cross": params.is_cross,
                        "result": result,
                    }
                )

            return {
                "success": True,
                "result": result,
                "coin": params.coin.upper(),
                "leverage": params.leverage,
                "is_cross": params.is_cross,
                "execution_time_ms": (time.time() - start_time) * 1000
            }
        except Exception as e:
            self.logger.error(f"update_leverage failed: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Show Hyperliquid agent-wallet delegation status (no keys exposed)',
        param_model=EmptyParams,
    )
    async def agent_status(self, params: EmptyParams) -> Dict[str, Any]:
        """Report whether a delegated agent/API wallet is configured for trading.

        Read-only and key-safe: never returns a private key. The on-chain
        approve_agent/revoke_agent lifecycle is a separate change (testnet-verified).
        """
        await self.ensure_initialized()
        credentials = await self._get_user_credentials()
        if not credentials:
            return {"success": False, "error": "Credentials not configured"}

        # The polyrob agent wallet, when configured, takes precedence in
        # _get_exchange_client — report the ACTUAL signer on that path, not the
        # DB delegation state (H4: that path has no approveAgent firewall).
        try:
            from core.wallet.factory import get_agent_wallet
            polyrob_wallet = get_agent_wallet()
        except Exception:
            polyrob_wallet = None
        from core.surfaces.owner_address import is_owner_tenant
        if polyrob_wallet is not None and is_owner_tenant(self._user_id):
            return {
                "success": True,
                "delegated": False,
                "signer": ("polyrob-wallet (derived key owns its own account; "
                           "approveAgent firewall NOT in place — H4, live "
                           "trading refuses to arm on this path)"),
                "master_address": polyrob_wallet.account_for("hyperliquid").address,
                "agent_address": None,
                "agent_name": None,
                "testnet": credentials.testnet,
            }

        aw = credentials.agent_wallet
        return {
            "success": True,
            "delegated": aw is not None,
            "signer": "agent" if aw else "master",
            "master_address": credentials.wallet_address,
            "agent_address": aw.address if aw else None,
            "agent_name": aw.name if aw else None,
            "testnet": credentials.testnet,
        }

    @BaseTool.action(
        'Delegate trading to a fresh Hyperliquid agent/API wallet (signed by the master key)',
        param_model=EmptyParams,
    )
    async def approve_agent(self, params: EmptyParams, execution_context=None) -> Dict[str, Any]:
        """Approve a freshly-generated agent wallet so trades can be signed without the
        master key. Signed by the MASTER key (the one place it's used). Persists the new
        agent wallet (never returns its private key). On-chain behavior is testnet-verified.
        """
        await self.ensure_initialized()

        # H11: approve_agent signs with the MASTER key to authorize a new trading agent —
        # a value-adjacent mutation. A forged/autonomous turn OR the owner kill-switch
        # must never reach it; the owner must drive delegation.
        from polyrob_markets.trade_gate import trade_turn_refusal
        refusal = trade_turn_refusal(execution_context, self)
        if refusal:
            return {"success": False, "error": refusal, "forged_turn_blocked": True}

        if not _has_sdk():
            return {"success": False, "error": "hyperliquid-python-sdk not installed"}

        credentials = await self._get_user_credentials()
        if not credentials or not getattr(credentials, "private_key", None):
            return {"success": False, "error": "Master wallet not configured (private key required to approve an agent)"}

        from eth_account import Account
        try:
            master = Account.from_key(credentials.private_key)
        except Exception:
            return {"success": False, "error": "invalid master private key"}

        name = getattr(params, "name", None)
        try:
            await self._get_info_client()
            exchange = Exchange(master, base_url=credentials.api_url, account_address=master.address)
            # SDK generates a fresh agent key and signs the approval with the master wallet.
            result, agent_key = await asyncio.to_thread(exchange.approve_agent, name)
            if not isinstance(result, dict) or result.get("status") != "ok":
                return {"success": False, "error": "Venue rejected the agent approval; credentials unchanged"}
            agent_addr = Account.from_key(agent_key).address
        except Exception as e:
            self.logger.error(f"approve_agent failed: {e}")
            return {"success": False, "error": f"approve_agent failed: {e}"}

        # Persist the new agent wallet (encrypted). Never reuse an old one (replay safety).
        from polyrob_markets.hyperliquid.models import AgentWallet
        agent_wallet = AgentWallet(address=agent_addr, private_key=agent_key, name=name)
        if self.db:
            await self.db.update_agent_wallet(self._user_id, agent_wallet)
            try:
                await self.db.audit_log(self._user_id, "approve_agent", tool_name="hyperliquid",
                                        details={"agent_address": agent_addr})
            except Exception:
                pass
        self._credentials_cache.clear()

        return {
            "success": True,
            "agent_address": agent_addr,
            "master_address": credentials.wallet_address,
            "note": "Agent wallet approved + persisted. Verify on testnet before live trading.",
        }

    @BaseTool.action(
        'Revoke the stored Hyperliquid agent key at the venue by replacing its approval',
        param_model=EmptyParams,
    )
    async def revoke_agent(self, params: EmptyParams, execution_context=None) -> Dict[str, Any]:
        """Replace the same named approval, discard its fresh key, then clear local state.

        Hyperliquid deregisters the previous key on a same-name approveAgent.
        The unnamed slot is replaced with name=None. A failed/unknown response
        preserves local metadata so the owner can reconcile or retry revocation.
        """
        await self.ensure_initialized()

        # H11: forged/autonomous turns OR the owner kill-switch cannot alter the trading
        # delegation state (parity with approve_agent).
        from polyrob_markets.trade_gate import trade_turn_refusal
        refusal = trade_turn_refusal(execution_context, self, entry=False)
        if refusal:
            return {"success": False, "error": refusal, "forged_turn_blocked": True}

        credentials = await self._get_user_credentials()
        if not credentials:
            return {"success": False, "error": "Credentials not configured"}
        agent = credentials.agent_wallet
        if agent is None:
            return {"success": False, "error": "No stored agent key identifies the approval to revoke"}
        if not self.db or not _has_sdk() or not credentials.private_key:
            return {"success": False, "error": "Revocation requires the master key, SDK and credential store"}
        from eth_account import Account
        try:
            master = Account.from_key(credentials.private_key)
            if master.address.lower() != credentials.wallet_address.lower():
                return {"success": False, "error": "Master key does not match the configured account"}
            exchange = Exchange(master, base_url=credentials.api_url, account_address=master.address)
            result, discarded_key = await asyncio.to_thread(exchange.approve_agent, agent.name or None)
            del discarded_key
        except Exception:
            return {"success": False, "error": "Revocation outcome is unknown; stored agent metadata retained for reconciliation"}
        if not isinstance(result, dict) or result.get("status") != "ok":
            return {"success": False, "error": "Venue rejected revocation; stored agent metadata retained"}
        await self.db.update_agent_wallet(self._user_id, None)
        self._credentials_cache.clear()
        return {"success": True, "revoked_address": agent.address,
                "note": "Venue accepted replacement of the previous agent approval. "
                        "The replacement key was discarded and local delegation was cleared."}

    # =========================================================================
    # Tool Discovery
    # =========================================================================

    def get_available_tools(self) -> List[Dict[str, Any]]:
        """Return list of available tools with their schemas"""
        if self._tools_cache:
            return self._tools_cache

        tools = [
            # Market Discovery
            {
                "name": "get_perpetual_markets",
                "description": "Get all available perpetual futures markets with metadata",
                "inputSchema": EmptyParams.model_json_schema(),
                "category": "market_discovery",
            },
            {
                "name": "get_spot_markets",
                "description": "Get all available spot trading pairs",
                "inputSchema": EmptyParams.model_json_schema(),
                "category": "market_discovery",
            },
            # Market Data
            {
                "name": "get_current_price",
                "description": "Get current mid price for a coin",
                "inputSchema": GetPriceParams.model_json_schema(),
                "category": "market_data",
            },
            {
                "name": "get_all_mids",
                "description": "Get all mid prices for all markets",
                "inputSchema": EmptyParams.model_json_schema(),
                "category": "market_data",
            },
            {
                "name": "get_orderbook",
                "description": "Get L2 orderbook with bid/ask levels",
                "inputSchema": GetOrderbookParams.model_json_schema(),
                "category": "market_data",
            },
            {
                "name": "get_funding_rate",
                "description": "Get current funding rate for a perpetual",
                "inputSchema": GetFundingParams.model_json_schema(),
                "category": "market_data",
            },
            # Portfolio
            {
                "name": "get_account_state",
                "description": "Get perpetuals account summary including margin and positions",
                "inputSchema": EmptyParams.model_json_schema(),
                "category": "portfolio",
                "requires_auth": True,
            },
            {
                "name": "get_spot_balances",
                "description": "Get spot token balances",
                "inputSchema": EmptyParams.model_json_schema(),
                "category": "portfolio",
                "requires_auth": True,
            },
            {
                "name": "get_open_orders",
                "description": "Get all open orders",
                "inputSchema": EmptyParams.model_json_schema(),
                "category": "portfolio",
                "requires_auth": True,
            },
            {
                "name": "get_fills",
                "description": "Get fill history",
                "inputSchema": GetFillsParams.model_json_schema(),
                "category": "portfolio",
                "requires_auth": True,
            },
            # Trading
            {
                "name": "place_limit_order",
                "description": "Place a limit order for perpetuals or spot",
                "inputSchema": PlaceLimitOrderParams.model_json_schema(),
                "category": "trading",
                "requires_auth": True,
                "requires_trading_enabled": True,
            },
            {
                "name": "place_market_order",
                "description": "Place a market order (marketable IOC at mid +/- slippage)",
                "inputSchema": PlaceMarketOrderParams.model_json_schema(),
                "category": "trading",
                "requires_auth": True,
                "requires_trading_enabled": True,
            },
            {
                "name": "cancel_order",
                "description": "Cancel an open order",
                "inputSchema": CancelOrderParams.model_json_schema(),
                "category": "trading",
                "requires_auth": True,
                "requires_trading_enabled": True,
            },
            {
                "name": "cancel_all_orders",
                "description": "Cancel all open orders",
                "inputSchema": CancelAllOrdersParams.model_json_schema(),
                "category": "trading",
                "requires_auth": True,
                "requires_trading_enabled": True,
            },
            {
                "name": "update_leverage",
                "description": "Update leverage for a perpetual market",
                "inputSchema": UpdateLeverageParams.model_json_schema(),
                "category": "trading",
                "requires_auth": True,
                "requires_trading_enabled": True,
            },
            {
                "name": "agent_status",
                "description": "Show agent-wallet delegation status (read-only, no keys)",
                "inputSchema": EmptyParams.model_json_schema(),
                "category": "account",
                "requires_auth": True,
                "requires_trading_enabled": False,
            },
            {
                "name": "approve_agent",
                "description": "Delegate trading to a fresh agent wallet (signed by the master key)",
                "inputSchema": EmptyParams.model_json_schema(),
                "category": "account",
                "requires_auth": True,
                "requires_trading_enabled": True,
            },
            {
                "name": "revoke_agent",
                "description": "Revoke the previous agent key by replacing its venue approval",
                "inputSchema": EmptyParams.model_json_schema(),
                "category": "account",
                "requires_auth": True,
                "requires_trading_enabled": True,
            },
        ]

        self._tools_cache = tools
        return tools

    async def execute_action(
        self,
        tool_name: str,
        arguments: Dict[str, Any]
    ) -> ExecutionResult:
        """Execute a tool action by name"""
        start_time = time.time()

        action_map = {
            "get_perpetual_markets": (self.get_perpetual_markets, EmptyParams),
            "get_spot_markets": (self.get_spot_markets, EmptyParams),
            "get_current_price": (self.get_current_price, GetPriceParams),
            "get_all_mids": (self.get_all_mids, EmptyParams),
            "get_orderbook": (self.get_orderbook, GetOrderbookParams),
            "get_funding_rate": (self.get_funding_rate, GetFundingParams),
            "get_account_state": (self.get_account_state, EmptyParams),
            "get_spot_balances": (self.get_spot_balances, EmptyParams),
            "get_open_orders": (self.get_open_orders, EmptyParams),
            "get_fills": (self.get_fills, GetFillsParams),
            "place_limit_order": (self.place_limit_order, PlaceLimitOrderParams),
            "place_market_order": (self.place_market_order, PlaceMarketOrderParams),
            "cancel_order": (self.cancel_order, CancelOrderParams),
            "cancel_all_orders": (self.cancel_all_orders, CancelAllOrdersParams),
            "update_leverage": (self.update_leverage, UpdateLeverageParams),
            "agent_status": (self.agent_status, EmptyParams),
            "approve_agent": (self.approve_agent, EmptyParams),
            "revoke_agent": (self.revoke_agent, EmptyParams),
        }

        if tool_name not in action_map:
            return ExecutionResult(
                success=False,
                error=f"Unknown tool: {tool_name}. Available: {', '.join(action_map.keys())}",
                tool_name=tool_name
            )

        method, param_class = action_map[tool_name]

        try:
            params = param_class(**arguments)
            result = await method(params)

            execution_time = (time.time() - start_time) * 1000

            return ExecutionResult(
                success=result.get("success", False),
                data=result,
                error=result.get("error"),
                tool_name=tool_name,
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            self.logger.error(f"Tool execution failed: {tool_name}: {e}")
            return ExecutionResult(
                success=False,
                error=f"Parameter validation failed: {str(e)}",
                tool_name=tool_name,
                execution_time_ms=execution_time
            )


# =============================================================================
# READ / TRADE SPLIT (additive)
# =============================================================================

# Wallet-free read actions. All are public /info reads (market data + account state by
# the MASTER address) plus the read-only agent_status. Trading actions stay on the gated
# 'hyperliquid' tool.
HL_READ_ACTIONS = frozenset({
    "get_perpetual_markets", "get_spot_markets", "get_current_price", "get_all_mids",
    "get_orderbook", "get_funding_rate", "get_account_state", "get_spot_balances",
    "get_open_orders", "get_fills", "agent_status",
})


class HyperliquidDataTool(HyperliquidTool):
    """Read-only Hyperliquid tool (no signing, delegatable). Exposes only HL_READ_ACTIONS;
    trade actions are filtered at registration, advertisement, and execution."""

    tool_id = "hyperliquid_data"
    name = "hyperliquid_data"
    description = "Hyperliquid perps/spot — read-only market data & account state (no signing)"

    def get_actions(self) -> Dict[str, Any]:
        return {k: v for k, v in super().get_actions().items() if k in HL_READ_ACTIONS}

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return [t for t in super().get_available_tools() if t.get("name") in HL_READ_ACTIONS]

    async def execute_action(self, tool_name: str, arguments: Dict[str, Any]) -> ExecutionResult:
        if tool_name not in HL_READ_ACTIONS:
            return ExecutionResult(
                success=False,
                error=f"'{tool_name}' is a trade action; use the gated 'hyperliquid' tool. "
                      f"'hyperliquid_data' is read-only.",
                tool_name=tool_name,
            )
        return await super().execute_action(tool_name, arguments)
