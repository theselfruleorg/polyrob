"""A2A Agent Card - Self-describing manifest of POLYROB's capabilities.

The Agent Card is hosted at /.well-known/agent.json following RFC 8615.
Other A2A-compliant agents use this to discover POLYROB's capabilities
and understand how to interact with it.

Reference: https://a2a-protocol.org/latest/topics/agent-discovery/
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, Request
import os
import logging

from core.version import get_version

logger = logging.getLogger(__name__)
router = APIRouter()


def _x402_price_usd() -> float:
    """Single x402 price source (F12) — keeps the card aligned with the live charge."""
    from modules.x402.x402_integration import get_x402_price_usd
    return get_x402_price_usd()


class AgentSkill(BaseModel):
    """A specific capability the agent can perform."""
    id: str = Field(..., description="Unique skill identifier")
    name: str = Field(..., description="Human-readable name")
    description: str = Field(..., description="Functional explanation")
    tags: Optional[List[str]] = Field(default_factory=list)
    examples: Optional[List[str]] = Field(default_factory=list, description="Example use cases")
    inputModes: Optional[List[str]] = Field(default=["text"], description="Supported input MIME types")
    outputModes: Optional[List[str]] = Field(default=["text"], description="Supported output MIME types")
    metadata: Optional[Dict[str, Any]] = None


class AgentCapabilities(BaseModel):
    """Agent capability flags."""
    streaming: bool = True  # SSE streaming support
    pushNotifications: bool = True  # Webhook callbacks
    stateTransitionHistory: bool = True  # Task state change history


class AgentProvider(BaseModel):
    """Organization providing this agent."""
    organization: str
    url: str


class SecurityScheme(BaseModel):
    """Security scheme definition (OpenAPI 3.2 compatible)."""
    type: str  # "apiKey", "http", "oauth2", "openIdConnect", "mutualTLS"
    scheme: Optional[str] = None  # For http: "bearer", "basic"
    bearerFormat: Optional[str] = None  # e.g., "JWT"
    name: Optional[str] = None  # For apiKey: header/query param name
    in_: Optional[str] = Field(None, alias="in")  # For apiKey: "header", "query", "cookie"
    description: Optional[str] = None

    class Config:
        populate_by_name = True


class AgentCard(BaseModel):
    """A2A Agent Card - Complete agent metadata.

    Hosted at /.well-known/agent.json for discovery.
    """
    # Core Identity
    name: str = "POLYROB"
    description: str = Field(
        default="AI automation agent with browser control, file system access, "
                "MCP integrations, and autonomous task execution capabilities."
    )
    url: str = Field(..., description="Base URL for A2A service endpoint")
    version: str = Field(default_factory=get_version)

    # Protocol
    protocolVersion: str = "1.0"  # A2A protocol version

    # Media Types
    defaultInputModes: List[str] = Field(
        default=["text/plain", "application/json", "image/png", "image/jpeg"]
    )
    defaultOutputModes: List[str] = Field(
        default=["text/plain", "application/json", "image/png", "application/pdf"]
    )

    # Capabilities
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)

    # Skills
    skills: List[AgentSkill] = Field(default_factory=list)

    # Provider
    provider: AgentProvider

    # Security (OpenAPI 3.2 compatible)
    securitySchemes: Dict[str, SecurityScheme] = Field(default_factory=dict)
    security: List[Dict[str, List[str]]] = Field(default_factory=list)

    # Extensions
    supportsAuthenticatedExtendedCard: bool = True

    # Pricing (custom extension for x402)
    pricing: Optional[Dict[str, Any]] = None

    # Metadata
    metadata: Optional[Dict[str, Any]] = None


def build_agent_card(request: Optional[Request] = None) -> AgentCard:
    """Build the Agent Card from current configuration.

    Args:
        request: Optional FastAPI request for URL detection

    Returns:
        Complete AgentCard instance
    """
    # Determine base URL
    base_url = os.environ.get("A2A_BASE_URL")
    if not base_url and request:
        # Construct from request
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("host", request.url.netloc)
        base_url = f"{scheme}://{host}"

    base_url = base_url or "http://localhost:9000"

    # Define skills
    skills = [
        AgentSkill(
            id="web-automation",
            name="Web Automation",
            description="Automate browser tasks: navigate websites, click elements, "
                       "fill forms, take screenshots, extract data from web pages.",
            tags=["browser", "automation", "scraping", "web"],
            examples=[
                "Go to example.com and take a screenshot",
                "Fill out the contact form with my information",
                "Extract all product prices from the page",
                "Navigate through a multi-step checkout process"
            ],
            inputModes=["text/plain", "image/png", "image/jpeg"],
            outputModes=["text/plain", "image/png", "application/json"]
        ),
        AgentSkill(
            id="file-management",
            name="File Management",
            description="Create, read, edit, and organize files in the workspace. "
                       "Supports various formats including text, JSON, CSV, and more.",
            tags=["filesystem", "documents", "files"],
            examples=[
                "Create a summary.txt with the meeting notes",
                "Read the config.json file and parse its contents",
                "Organize downloaded files by date",
                "Convert CSV data to JSON format"
            ],
            inputModes=["text/plain", "application/json"],
            outputModes=["text/plain", "application/json", "text/csv"]
        ),
        AgentSkill(
            id="research",
            name="Research & Analysis",
            description="Search the web, analyze data, compile reports, and synthesize "
                       "information from multiple sources.",
            tags=["research", "search", "analysis", "reports"],
            examples=[
                "Research the top 5 competitors in AI automation",
                "Find recent news about cryptocurrency regulations",
                "Analyze the sentiment of customer reviews",
                "Create a comparison report of SaaS tools"
            ],
            inputModes=["text/plain"],
            outputModes=["text/plain", "application/json", "text/markdown"]
        ),
        AgentSkill(
            id="mcp-integration",
            name="MCP Tool Integration",
            description="Access external services via Model Context Protocol. "
                       "Connect to databases, APIs, and other services through MCP.",
            tags=["mcp", "integration", "api", "external-services"],
            examples=[
                "Use the anysite tool to scrape the article content",
                "Query the connected database for sales data",
                "Send an email via the configured SMTP service",
                "Fetch data from the weather API"
            ],
            inputModes=["text/plain", "application/json"],
            outputModes=["text/plain", "application/json"]
        ),
        AgentSkill(
            id="task-planning",
            name="Task Planning & Execution",
            description="Break down complex tasks into steps, plan execution strategy, "
                       "and autonomously complete multi-step workflows.",
            tags=["planning", "workflow", "automation", "multi-step"],
            examples=[
                "Plan and execute a market research project",
                "Set up a new project structure with files and configs",
                "Create and populate a spreadsheet with collected data",
                "Automate a daily reporting workflow"
            ],
            inputModes=["text/plain"],
            outputModes=["text/plain", "application/json"]
        )
    ]

    # Security schemes
    security_schemes = {
        "x402": SecurityScheme(
            type="apiKey",
            name="X-PAYMENT",
            in_="header",
            description="x402 cryptocurrency payment. Pay-per-request, no account required. See /api/x402/pricing for details."
        ),
        "apiKey": SecurityScheme(
            type="apiKey",
            name="X-API-KEY",
            in_="header",
            description="API key for programmatic access. Create at POST /api/auth/api-keys (requires DEN token)."
        ),
        "bearer": SecurityScheme(
            type="http",
            scheme="bearer",
            bearerFormat="JWT",
            description="JWT token from wallet SIWE authentication. For web UI users."
        )
    }

    # Security requirements - API key is easiest for agents
    security = [
        {"apiKey": []},  # Recommended: API key (simple, persistent)
        {"x402": []},    # Alternative: pay-per-request (no account)
        {"bearer": []}   # Alternative: JWT (for web users)
    ]

    # Pricing information (x402 extension)
    pricing = {
        "model": "pay-per-request",
        "description": "Pay only for what you use. No subscription required.",
        "authentication_options": {
            "api_key": {
                "description": "Recommended for AI agents. Create once, use forever.",
                "how_to_get": "1) Login with wallet at /api/auth/nonce + /api/auth/verify, 2) POST /api/auth/api-keys",
                "requires": "DEN token ownership",
                "header": "X-API-KEY: rob_xxx..."
            },
            "x402": {
                "description": "Pay-per-request with crypto. No account needed.",
                "how_to_use": (
                    "Standard x402 flow: 1) send your request; 2) on HTTP 402 read the "
                    "payment requirements; 3) retry with an X-PAYMENT header (base64 "
                    "EIP-3009 authorization). Settlement is handled automatically."
                ),
                "per_request_usd": _x402_price_usd(),
                "supported_chains": ["base", "ethereum"],
                "supported_assets": ["usdc", "usdt", "eth"],
                "payment_address": os.environ.get("X402_PAYMENT_RECIPIENT", os.environ.get("X402_PAYMENT_ADDRESS", "")),
                "facilitator": os.environ.get("X402_FACILITATOR_URL", "") or "Direct signature verification"
            }
        },
        "credits": {
            "enabled": True,
            "credit_cost_usd": 0.01,
            "session_credits": 1,
            "description": "For registered users with pre-purchased credits"
        }
    }

    # Instance branding: reuse the webview branding seam (POLYROB_BRAND_URL /
    # POLYROB_TERMS_URL / POLYROB_PRIVACY_URL / POLYROB_SUPPORT_URL) so an
    # instance sets its brand once; default is instance-neutral (the base URL).
    brand_url = os.environ.get("POLYROB_BRAND_URL", base_url).strip().rstrip("/")

    return AgentCard(
        url=f"{base_url}/a2a",
        skills=skills,
        provider=AgentProvider(
            organization="The Self Rule",
            url=brand_url
        ),
        securitySchemes=security_schemes,
        security=security,
        pricing=pricing,
        metadata={
            "contact": os.environ.get("POLYROB_SUPPORT_URL", brand_url).strip(),
            "documentation": f"{base_url}/docs",
            "terms_of_service": os.environ.get("POLYROB_TERMS_URL", f"{brand_url}/terms").strip(),
            "privacy_policy": os.environ.get("POLYROB_PRIVACY_URL", f"{brand_url}/privacy").strip()
        }
    )


@router.get("/.well-known/agent.json", response_model=AgentCard)
async def get_agent_card_wellknown(request: Request) -> AgentCard:
    """Return the Agent Card at RFC 8615 well-known path.

    This is the primary discovery endpoint for A2A agents.
    """
    logger.info("Agent Card requested at /.well-known/agent.json")
    return build_agent_card(request)


@router.get("/a2a/agent-card", response_model=AgentCard)
async def get_agent_card_api(request: Request) -> AgentCard:
    """Return the Agent Card at API path.

    Alternative endpoint for programmatic access.
    """
    return build_agent_card(request)


@router.get("/a2a/extended-card", response_model=AgentCard)
async def get_extended_agent_card(request: Request) -> AgentCard:
    """Return extended Agent Card for authenticated clients.

    May include additional capabilities or reduced pricing
    for authenticated users.
    """
    # Build base card
    card = build_agent_card(request)

    # Check authentication
    user_id = getattr(request.state, 'user_id', None)
    tier = getattr(request.state, 'tier', 'free')

    if user_id and tier != 'free':
        # Add tier-specific metadata
        card.metadata = card.metadata or {}
        card.metadata['authenticated'] = True
        card.metadata['tier'] = tier

    return card
