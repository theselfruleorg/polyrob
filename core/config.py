"""Location: core/config.py"""

"""Configuration management for the bot."""

import os
from pathlib import Path
import logging
from typing import Any, Optional, Dict, List
from pydantic import Field, field_validator, model_validator, PrivateAttr, BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.config_policy import _mode_capability_default
from core.env import parse_bool


# ---------------------------------------------------------------------------
# AgentConfig — pure-agent settings that rob-core needs to run standalone.
#
# Lives in core so rob-core has a config surface that imports zero
# server/billing/multi-tenant fields. ServerConfig below inherits from this
# and adds the server-scope fields (auth, billing, blockchain, x402, etc.).
# Existing `from core.config import BotConfig` call sites keep working via
# the `BotConfig = ServerConfig` alias at the bottom of this file.
# When the server is extracted to a separate repo, ServerConfig moves to
# polyrob-platform.
#
# Conservative scope: only fields that are unambiguously agent-scope are
# declared here. Anything mixed (auto_*, twitter_*, blockchain, alchemy, MCP
# config dict, browser/agent/controller dicts) stays in ServerConfig until the
# Phase B repo split forces the full classification.
# ---------------------------------------------------------------------------


class AgentConfig(BaseSettings):
    """Pure-agent configuration — LLM keys, sessions, paths, logging."""

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
        validate_assignment=True,
    )

    # Base settings
    base_dir: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    environment: str = "development"

    # Data paths
    data_dir: str = Field(default="data")
    # R-2 B1 (2026-07-17): default aligned with the file the app ACTUALLY opens
    # (modules/database/database_manager.py derives <data_dir>/database/bot.db).
    # The old "data/bot.db" default was a decoy nothing ever opened.
    db_path: str = Field(default="data/database/bot.db")

    # Logging
    log_level: str = Field('INFO', alias='LOG_LEVEL')

    # LLM API Keys (no model selection here — that lives in
    # modules.llm.llm_client_registry.DEFAULT_MODELS).
    openai_api_key: Optional[str] = Field(default=None, alias='OPENAI_API_KEY')
    anthropic_api_key: Optional[str] = Field(default=None, alias='ANTHROPIC_API_KEY')
    gemini_api_key: Optional[str] = Field(default=None, alias='GEMINI_API_KEY')
    deepseek_api_key: Optional[str] = Field(default=None, alias='DEEPSEEK_API_KEY')
    deepseek_api_url: Optional[str] = Field(default="https://api.deepseek.com/v1", alias='DEEPSEEK_API_URL')
    openrouter_api_key: Optional[str] = Field(default=None, alias='OPENROUTER_API_KEY')
    openrouter_site_url: Optional[str] = Field(default="", alias='OPENROUTER_SITE_URL')
    openrouter_site_name: Optional[str] = Field(default="POLYROB AI Agent", alias='OPENROUTER_SITE_NAME')
    # NVIDIA NIM (OpenAI-compatible; free hosted inference for Kimi/Llama/etc.)
    nvidia_api_key: Optional[str] = Field(default=None, alias='NVIDIA_API_KEY')
    nvidia_api_url: Optional[str] = Field(default="https://integrate.api.nvidia.com/v1", alias='NVIDIA_API_URL')
    perplexity_api_key: Optional[str] = Field(default=None, alias='PERPLEXITY_API_KEY')

    # Default agent persona
    characters_dir: str = Field(default="agents/personality/characters", alias='CHARACTERS_DIR')
    default_character: str = Field(default="rob", alias='DEFAULT_CHARACTER')

    # Session memory management
    session_ttl_seconds: int = Field(default=86400, alias='SESSION_TTL_SECONDS')
    max_sessions_in_memory: int = Field(default=100, alias='MAX_SESSIONS_IN_MEMORY')
    session_cleanup_interval: int = Field(default=600, alias='SESSION_CLEANUP_INTERVAL')
    max_sessions_per_user: int = Field(default=10, alias='MAX_SESSIONS_PER_USER')
    # A session stuck in 'created' that never ran has no run-path activity timestamp,
    # so the TTL/LRU GC (which keys off _session_last_activity) never retires it — yet it
    # still counts toward max_sessions_per_user. This shorter TTL (keyed off created_at)
    # retires such never-run sessions so they stop consuming the per-user limit.
    created_session_ttl_seconds: int = Field(default=3600, alias='CREATED_SESSION_TTL_SECONDS')

    # Sub-agent delegation (C-DELEG: ON by default with conservative caps below —
    # depth=1, max 3 concurrent, 600s/900s timeouts. Set SUB_AGENTS_ENABLED=false to opt out.)
    sub_agents_enabled: bool = Field(default=True, alias='SUB_AGENTS_ENABLED')
    sub_agent_timeout: int = Field(default=600, alias='SUB_AGENT_TIMEOUT')
    parallel_subtasks_timeout: int = Field(default=900, alias='PARALLEL_SUBTASKS_TIMEOUT')
    max_concurrent_sub_agents: int = Field(default=3, alias='MAX_CONCURRENT_SUB_AGENTS')
    max_sub_agent_depth: int = Field(default=1, alias='MAX_SUB_AGENT_DEPTH')

    # Agent-side validators (moved out of BotConfig)
    @field_validator('session_ttl_seconds')
    @classmethod
    def _validate_ttl(cls, v: int) -> int:
        if v < 60:
            raise ValueError("Session TTL must be at least 60 seconds")
        if v > 604800:
            logging.getLogger(__name__).warning(
                f"Session TTL {v}s is very high (>7 days) - may cause memory issues"
            )
        return v

    @field_validator('max_sessions_in_memory')
    @classmethod
    def _validate_max_sessions(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Must allow at least 1 session in memory")
        if v > 1000:
            logging.getLogger(__name__).warning(
                f"Max sessions {v} is very high - may cause memory issues"
            )
        return v

    @field_validator('session_cleanup_interval')
    @classmethod
    def _validate_cleanup_interval(cls, v: int) -> int:
        if v < 10:
            raise ValueError("Cleanup interval must be at least 10 seconds")
        if v > 3600:
            logging.getLogger(__name__).warning(
                f"Cleanup interval {v}s is very high (>1 hour)"
            )
        return v

    @model_validator(mode="before")
    @classmethod
    def _coerce_bool_env_fields(cls, data):
        """POLYROB falsey-set semantics for EVERY bool field (P0 finalization).

        pydantic's native bool parser rejects the repo's own canonical falsey
        tokens (none/off/false/0/no/''), so an env value like ``MCP_ENABLED=none``
        crashed ServerConfig()/AgentConfig() at construction. Route every string
        destined for a bool-annotated field through ``core.env.parse_bool``. This
        covers ALL bool fields by annotation (not an enumerated subset), so a
        newly-added bool can never regress the way this bug did — it supersedes the
        old per-field ``_coerce_mem`` validator.
        """
        if not isinstance(data, dict):
            return data
        for name, field in cls.model_fields.items():
            if field.annotation is bool:
                for key in (name, field.alias):
                    if key and key in data and isinstance(data[key], str):
                        data[key] = parse_bool(data[key], False)
        return data

    def available_providers(self) -> list:
        """Provider names whose API key is set on this config, in PROFILES order.

        The config-object companion to ``modules.llm.profiles.providers_with_keys``
        (which reads an env mapping). One SSOT for "which providers have a key" for
        callers that hold a config (banner, ``rob model list``).
        """
        from modules.llm.profiles import PROFILES
        return [
            name for name in PROFILES
            if getattr(self, f"{name}_api_key", None)
        ]


class ServerConfig(AgentConfig):
    """Full server-side configuration: agent fields (inherited) + server-scope."""

    # NOTE: model_config inherited from AgentConfig. The fields below are
    # everything that depends on the server stack (auth, billing, payments,
    # blockchain, multi-tenant, Telegram/Twitter/Gmail). When rob-server is
    # extracted as a separate package, this class moves to polyrob-platform.

    # Bot settings - Required fields with aliases
    admin_ids: List[int] = Field(default_factory=list, alias='ADMIN_IDS')
    moderator_ids: List[int] = Field(default_factory=list, alias='MODERATOR_IDS')
    support_chat_id: Optional[int] = Field(None, alias='SUPPORT_CHAT_ID')
    feedback_chat_id: Optional[int] = Field(None, alias='FEEDBACK_CHAT_ID')
    auto_knowledge_retention_days: int = Field(default=7, alias='AUTO_KNOWLEDGE_RETENTION_DAYS')
    auto_metrics_enabled: bool = Field(default=True, alias='AUTO_METRICS_ENABLED')
    auto_safe_mode: bool = Field(default=True, alias='AUTO_SAFE_MODE')
    
    # HITL (Human-in-the-Loop) Configuration
    hitl_mode: str = Field(default="chat", alias='HITL_MODE')  # "chat" | "block" | "off"
    destructive_action_policy: str = Field(default="none", alias='DESTRUCTIVE_ACTION_POLICY')  # "confirm_phrase" | "soft_wait" | "none"
    interrupt_window_seconds: int = Field(default=5, alias='INTERRUPT_WINDOW_SECONDS')

    # OCR Service Configuration
    services_ocr_enabled: bool = Field(default=True, alias='SERVICES_OCR_ENABLED')
    services_ocr_tesseract_path: str = Field(default="", alias='SERVICES_OCR_TESSERACT_PATH')
    services_ocr_dpi: int = Field(default=300, alias='SERVICES_OCR_DPI')
    services_ocr_languages: List[str] = Field(default_factory=lambda: ["eng"], alias='SERVICES_OCR_LANGUAGES')
    services_ocr_use_llm_correction: bool = Field(default=True, alias='SERVICES_OCR_USE_LLM_CORRECTION')
    services_ocr_temp_dir: str = Field(default="data/temp", alias='SERVICES_OCR_TEMP_DIR')
    
    # LLM API keys live on AgentConfig (parent) — see top of file.

    # Pinecone retired: cross-session semantic recall is now local (sqlite-vec memory
    # backend). The local embedding model is configured via get_embedding_config().

    # data_dir, db_path live on AgentConfig (parent).

    # Cache Configuration
    cache_size: int = Field(default=1000, alias='CACHE_SIZE')
    cache_ttl: int = Field(default=3600)
    
    # Browser Use Configuration
    browser_use_logging_level: str = Field('info', description="Browser Use logging level", alias='BROWSER_USE_LOGGING_LEVEL')
    anonymized_telemetry: bool = Field(True, description="Enable anonymized telemetry", alias='ANONYMIZED_TELEMETRY')
    max_browser_contexts: int = Field(default=25, alias='MAX_BROWSER_CONTEXTS', description="Maximum concurrent browser contexts")
    max_contexts_per_session: int = Field(default=2, alias='MAX_CONTEXTS_PER_SESSION', description="Maximum browser contexts per session")
    browser_headless: bool = Field(default=True, alias='BROWSER_HEADLESS', description="Run browser in headless mode")
    browser_context_timeout: float = Field(default=30.0, alias='BROWSER_CONTEXT_TIMEOUT', description="Timeout (s) for creating a browser context")
    browser_stale_timeout: float = Field(default=300.0, alias='BROWSER_STALE_TIMEOUT', description="Idle timeout (s) before a context is considered stale")
    browser_enable_pooling: bool = Field(default=True, alias='BROWSER_ENABLE_POOLING', description="Enable browser context pooling/reuse")
    browser_wait_queue_backstop_interval: float = Field(default=15.0, alias='BROWSER_WAIT_QUEUE_BACKSTOP_INTERVAL', description="Poll interval (s) for the wait-queue backstop loop (primary path resolves waiters synchronously on release)")

    # log_level lives on AgentConfig (parent).
    assistant_id: Optional[str] = Field(None, alias='ASSISTANT_ID')
    
    # Document processor settings
    doc_max_file_size: int = Field(default=20 * 1024 * 1024, description="Maximum file size for document processor (20MB)")
    doc_max_text_length: int = Field(default=100000, description="Maximum text length for document processor")
    doc_chunk_size: int = Field(default=4096, description="Chunk size for document processor")
    doc_cache_ttl: int = Field(default=3600, description="Cache TTL for document processor")
    
    # characters_dir, default_character live on AgentConfig (parent).

    # Profile monitoring configuration
    profiles_to_monitor: List[str] = Field(default_factory=list, description="List of Twitter profiles to monitor")
    update_interval_seconds: int = Field(360, description="Interval between profile updates in seconds")
    
    # API configuration
    api_host: str = Field('127.0.0.1', alias='API_HOST')
    api_port: int = Field(9000, alias='API_PORT')
    api_auth_token: Optional[str] = Field(None, alias='API_AUTH_TOKEN')
    
    # Twitter configuration with correct env var names
    twitter_api_key: Optional[str] = Field(None, alias='TWITTER_API_KEY')
    twitter_api_secret_key: Optional[str] = Field(None, alias='TWITTER_API_SECRET_KEY')  # Match env var
    twitter_access_token: Optional[str] = Field(None, alias='TWITTER_ACCESS_TOKEN')
    twitter_access_token_secret: Optional[str] = Field(None, alias='TWITTER_ACCESS_TOKEN_SECRET')
    twitter_bearer_token: Optional[str] = Field(None, alias='TWITTER_BEARER_TOKEN')
    twitter_bot_user_id: Optional[str] = Field(None, alias='TWITTER_BOT_USER_ID')
    twitter_bot_username: Optional[str] = Field(None, alias='TWITTER_BOT_USERNAME')
    
    # Gmail configuration
    gmail_email: Optional[str] = Field(None, description="Gmail email address", alias='GMAIL_EMAIL')
    gmail_app_password: Optional[str] = Field(None, description="Gmail app password", alias='GMAIL_APP_PASSWORD')
    gmail_imap_server: str = Field('imap.gmail.com', description="Gmail IMAP server", alias='GMAIL_IMAP_SERVER')
    gmail_smtp_server: str = Field('smtp.gmail.com', description="Gmail SMTP server", alias='GMAIL_SMTP_SERVER')
    gmail_smtp_port: int = Field(587, description="Gmail SMTP port", alias='GMAIL_SMTP_PORT')
    
    # perplexity_api_key lives on AgentConfig (parent) — see top of file.

    # MCP (Model Context Protocol) configuration
    mcp: Optional[Dict[str, Any]] = Field(
        default=None,
        description="MCP server configuration"
    )
    
    # MCP environment variable bridging
    mcp_enabled: bool = Field(default=False, alias='MCP_ENABLED', description="Enable MCP service globally")
    mcp_servers_config: Optional[str] = Field(default=None, alias='MCP_SERVERS_CONFIG', description="JSON string containing MCP servers configuration")

    # Sub-agent config lives on AgentConfig (parent).

    # Rate Limiting
    rate_limit_window: int = Field(default=60, description="Rate limit window in seconds")
    rate_limit_max_requests: int = Field(default=30, description="Maximum requests per window")
    rate_limit: int = Field(
        default=1,
        description="Rate limit in seconds"
    )
    
    # Goals and Behavior
    goals: Dict[str, Any] = Field(
        default_factory=dict,
        description="Bot goals and behavior configuration"
    )
    
    # Telemetry
    telemetry_app_url: str = "https://eu.i.posthog.com"
    telemetry_app_port: int = Field(8080, alias='TELEMETRY_APP_PORT')

    # Legacy fields - kept for compatibility but not used
    # These can be removed in a future version

    # SSL verification
    ssl_verify: bool = Field(
        True,
        description="Whether to verify SSL certificates",
        alias='SSL_VERIFY'
    )

    # Memory feature flags — must be declared as real fields so pydantic-settings
    # reads the env var (getattr on an undeclared key always returns the default,
    # silently ignoring the environment — the Task 0.3 landmine).
    # NOTE: these bool flags (like every bool field here) are coerced through the
    # POLYROB falsey-set by AgentConfig._coerce_bool_env_fields — no per-field
    # validator needed (that enumerated approach was the source of the "only 5
    # fields coerced" P0).
    HIERARCHICAL_MEMORY_ENABLED: bool = True
    COMPACTION_ENABLED: bool = True
    SEMANTIC_RETRIEVAL_ENABLED: bool = True
    REFLECTION_ENABLED: bool = True
    FORGETTING_ENABLED: bool = True

    # New fields from the code block
    owner_id: Optional[int] = Field(None, description="Admin user ID")
    whitelisted_chats: List[int] = Field(default_factory=list, description="List of whitelisted chats")
    roles: Dict[str, Dict[str, bool]] = Field(default_factory=lambda: {
        'super_admin': {
            'all_permissions': True
        },
        'admin': {
            'use_bot': True,
            'manage_users': True,
            'manage_modes': True,
            'manage_knowledge': True,
            'manage_prompts': True
        },
        'moderator': {
            'use_bot': True,
            'manage_users': False,
            'manage_modes': True,
            'manage_knowledge': False,
            'manage_prompts': False
        },
        'user': {
            'use_bot': True,
            'manage_users': False,
            'manage_modes': False,
            'manage_knowledge': False,
            'manage_prompts': False
        }
    })

    # Add model configuration
    model_cache_dir: Optional[str] = Field(
        default=None,
        description="Directory to cache model files"
    )

    # Embedding Configuration
    embedding: Dict[str, Any] = Field(
        default_factory=dict,
        description="Embedding model configuration"
    )

    # Session memory management + validators live on AgentConfig (parent).

    # Add after the existing config methods in BotConfig class

    def get_embedding_config(self) -> Dict[str, Any]:
        """Get embedding model configuration."""
        # Check if embedding configuration is provided in config
        if hasattr(self, 'embedding') and isinstance(self.embedding, dict):
            config = self.embedding.copy()
        else:
            # Use field-based configuration
            config = {
                'model_name': getattr(self, 'embedding_model_name', 'sentence-transformers/all-MiniLM-L6-v2'),
                'dimension': getattr(self, 'embedding_dimension', 384),
                'max_seq_length': getattr(self, 'embedding_max_seq_length', 128)
            }
        
        # Add default values for missing keys
        config.setdefault('model_name', 'sentence-transformers/all-MiniLM-L6-v2')
        config.setdefault('dimension', getattr(self, 'embedding_dimension', 384))
        config.setdefault('max_seq_length', 128)
        config.setdefault('normalize_embeddings', True)
        config.setdefault('cache_folder', str(Path(self.data_dir) / 'models'))
        config.setdefault('device', 'cpu')
        
        return config

    def get_ocr_config(self) -> Dict[str, Any]:
        """Get OCR service configuration."""
        return {
            'enabled': self.services_ocr_enabled,
            'tesseract_path': self.services_ocr_tesseract_path,
            'dpi': self.services_ocr_dpi,
            'languages': self.services_ocr_languages,
            'use_llm_correction': self.services_ocr_use_llm_correction,
            'temp_dir': self.services_ocr_temp_dir
        }

    # Add these new fields to model_config
    embedding_model_name: str = Field(
        default='sentence-transformers/all-MiniLM-L6-v2',
        description="Name of the sentence transformer model to use",
        alias='EMBEDDING_MODEL_NAME'
    )
    
    embedding_dimension: int = Field(
        default=384,
        description="Dimension of embeddings",
        alias='EMBEDDING_DIMENSION'
    )
    
    embedding_max_seq_length: int = Field(
        default=128,
        description="Maximum sequence length for embeddings",
        alias='EMBEDDING_MAX_SEQ_LENGTH'
    )

    # Add validators for new fields
    @field_validator('embedding_dimension')
    def validate_embedding_dimension(cls, v: int) -> int:
        """Validate embedding dimension."""
        if v <= 0:
            raise ValueError("Embedding dimension must be positive")
        return v

    @field_validator('embedding_max_seq_length')
    def validate_embedding_max_seq_length(cls, v: int) -> int:
        """Validate max sequence length."""
        if v <= 0:
            raise ValueError("Max sequence length must be positive")
        return v
        
    @field_validator('services_ocr_dpi')
    def validate_ocr_dpi(cls, v: int) -> int:
        """Validate OCR DPI setting."""
        if v < 72:
            raise ValueError("OCR DPI must be at least 72")
        return v

    # Private attributes
    _logger: logging.Logger = PrivateAttr()
    _env_path: Path = PrivateAttr()
    _is_initialized: bool = PrivateAttr(default=False)


    @field_validator('openai_api_key', 'anthropic_api_key', 'alchemy_api_key')
    def validate_api_keys(cls, v: Optional[str], info) -> Optional[str]:
        """Validate API keys."""
        if v is None:
            return None
            
        v = v.strip()
        # Basic validation - check if it looks like a placeholder
        if v.lower() in ['your-openai-key', 'your-anthropic-key', 'your-api-key', 'your-alchemy-key']:
            return None
            
        # Basic length check
        if len(v) < 20:  # Most API keys are longer than 20 chars
            return None
            
        return v

    def __init__(self, **kwargs):
        """Initialize configuration."""
        # Initialize with pydantic settings
        super().__init__(**kwargs)
        self._ensure_directories()
        self._load_api_keys()
        self._build_mcp_config_from_env()
        self._is_initialized = True
        
    @property
    def is_initialized(self) -> bool:
        """Check if config is initialized."""
        return self._is_initialized
    
    def _build_mcp_config_from_env(self) -> None:
        """Build MCP configuration from JSON file or environment variables.

        Priority:
        1. Programmatic config (self.mcp already set)
        2. Local file-first overlay (~/.polyrob/mcp.json, ./.polyrob/mcp.json) — project wins
        3. JSON file (config/mcp_config.json)
        4. Environment variable (MCP_SERVERS_CONFIG)
        """
        import json
        from pathlib import Path

        # Priority 1: Use config.mcp if already provided (programmatic)
        if self.mcp is not None:
            return

        from tools.mcp.config import get_default_mcp_config, MCPConfig, load_local_mcp_servers

        # File-first local overlay (single-user mode). When present these enable MCP
        # even if MCP_ENABLED is unset, and override base servers on name clash.
        local_servers = load_local_mcp_servers()

        # Only build config if MCP is enabled OR local files supplied servers.
        # Proposal 013 (T2): also build under effective AUTONOMY_MODE=autonomous
        # (single-owner instance) via _mode_capability_default (top-level import
        # since WS-1 ph4 — core.config_policy is core-tier and light). Guarded:
        # any resolution failure must never break config construction.
        try:
            _mode_default = _mode_capability_default("MCP_ENABLED")
        except Exception:
            _mode_default = False
        if not (self.mcp_enabled or _mode_default) and not local_servers:
            return

        # Priority 3: Load from config/mcp_config.json if it exists
        mcp_config = None
        mcp_config_path = Path(self.base_dir) / "config" / "mcp_config.json"
        if mcp_config_path.exists():
            try:
                with open(mcp_config_path, 'r') as f:
                    mcp_data = json.load(f)
                mcp_config = MCPConfig(**mcp_data)
                print(f"✅ Loaded MCP config from {mcp_config_path}")
                print(f"   Servers: {list(mcp_config.servers.keys())}")
            except Exception as e:
                print(f"❌ Failed to load MCP config from {mcp_config_path}: {e}")
                mcp_config = None

        # Priority 4: Build from defaults + environment variable (fallback)
        if mcp_config is None:
            mcp_config = get_default_mcp_config()
            mcp_config.enabled = True
            if self.mcp_servers_config:
                try:
                    servers_config = json.loads(self.mcp_servers_config)
                    for server_name, server_config in servers_config.items():
                        mcp_config.servers[server_name] = server_config
                    print(f"✅ Loaded MCP servers from environment variable")
                    print(f"   Servers: {list(mcp_config.servers.keys())}")
                except (json.JSONDecodeError, TypeError) as e:
                    print(f"❌ Failed to parse MCP_SERVERS_CONFIG: {e}")

        # Priority 2 (overlay): local .polyrob/mcp.json servers win on name clash.
        if local_servers:
            for name, server_config in local_servers.items():
                mcp_config.servers[name] = server_config
            mcp_config.enabled = True
            print(f"✅ Overlaid {len(local_servers)} local MCP server(s) from .polyrob/mcp.json")

        self.mcp = mcp_config.model_dump()
    
    def _ensure_directories(self) -> None:
        """Ensure required directories exist.

        Anchor every path to base_dir BEFORE creating anything. Construction runs
        during BotConfig() — in the CLI that is BEFORE bootstrap reassigns
        data_dir=.rob — so a relative default ("data", "data/bot.db", "data/temp")
        must not materialize as a stray ./data tree in the caller's CWD.
        """
        # Runtime isolation (doc 01 T2): when POLYROB_DATA_DIR is set (the server
        # data-home), anchor the relative runtime data paths there — OUTSIDE the
        # code tree — instead of base_dir (the code root). Read via os.getenv only
        # (BotConfig.get is a getattr that ignores the env). The CLI/local path is
        # unaffected: bootstrap reassigns data_dir to an absolute .rob AFTER
        # construction, and these paths are already absolute by then. Fall back to
        # base_dir anchoring (legacy) when unset.
        data_anchor = os.getenv("POLYROB_DATA_DIR") or self.base_dir

        # Convert relative paths to absolute FIRST (so the mkdirs below never
        # resolve against CWD).
        if not Path(self.db_path).is_absolute():
            self.db_path = str(Path(data_anchor) / self.db_path)
        if not Path(self.data_dir).is_absolute():
            self.data_dir = str(Path(data_anchor) / self.data_dir)
        if not Path(self.services_ocr_temp_dir).is_absolute():
            self.services_ocr_temp_dir = str(Path(data_anchor) / self.services_ocr_temp_dir)

        # Now create the (absolute) directories. Fail-open on a read-only install
        # dir (R1: pip site-packages) — the CLI reassigns data_dir to .rob anyway.
        try:
            for path in [self.data_dir, Path(self.db_path).parent, self.services_ocr_temp_dir]:
                Path(path).mkdir(parents=True, exist_ok=True)
            for subdir in ["simulations", "characters", "logs", "cache", "knowledge"]:
                Path(self.data_dir, subdir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logging.getLogger("core.config").warning("could not create data dirs: %s", e)

    def _setup_logger(self) -> None:
        """Set up logging configuration."""
        self._logger = logging.getLogger("config")
        
        # Set up console handler
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter(
            '[%(asctime)s] %(levelname)s:%(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        
        # Set up file handler — anchor to base_dir, never the caller's CWD.
        log_dir = Path(self.base_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / "config.log",
            encoding='utf-8'
        )
        file_handler.setFormatter(console_formatter)
        
        # Configure logger
        self._logger.setLevel(getattr(logging, self.log_level.upper()))
        if not self._logger.handlers:
            self._logger.addHandler(console_handler)
            self._logger.addHandler(file_handler)
        self._logger.propagate = False

    def _log_config_details(self) -> None:
        """Log configuration details safely."""
        self._logger.debug(f"Environment: {self.environment}")
        self._logger.debug(f"Log Level: {self.log_level}")
        self._logger.debug(f"DB Path: {self.db_path}")
        self._logger.debug(f"Cache Size: {self.cache_size}")
        # Model selection handled by llm_client_registry.DEFAULT_MODELS
        self._logger.debug(f"OCR Service Enabled: {self.services_ocr_enabled}")
        
        # Log existence of sensitive fields without revealing values
        self._logger.debug(f"Anthropic API Key exists: {bool(self.anthropic_api_key)}")
        self._logger.debug(f"OpenAI API Key exists: {bool(self.openai_api_key)}")
        self._logger.debug(f"Alchemy API Key exists: {bool(self.alchemy_api_key)}")

    def load_twitter_config(self) -> None:
        """Load Twitter configuration from environment."""
        if all([
            self.twitter_api_key,
            self.twitter_api_secret_key,  # Match the field name
            self.twitter_access_token,
            self.twitter_access_token_secret,
            self.twitter_bearer_token
        ]):
            self._logger.info("Twitter API credentials configured")
        else:
            missing = []
            if not self.twitter_api_key:
                missing.append("TWITTER_API_KEY")
            if not self.twitter_api_secret_key:  # Match the field name
                missing.append("TWITTER_API_SECRET_KEY")  # Match env var name
            if not self.twitter_access_token:
                missing.append("TWITTER_ACCESS_TOKEN")
            if not self.twitter_access_token_secret:
                missing.append("TWITTER_ACCESS_TOKEN_SECRET")
            if not self.twitter_bearer_token:
                missing.append("TWITTER_BEARER_TOKEN")
            
            if missing:
                self._logger.warning(f"Missing Twitter credentials: {', '.join(missing)}")

    @property
    def is_development(self) -> bool:
        """Check if environment is development."""
        return self.environment.lower() == 'development'

    # Validators
    @field_validator('api_port', 'telemetry_app_port')
    def validate_port(cls, v: int) -> int:
        """Validate port number is in valid range."""
        if not 1 <= v <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        return v

    @field_validator('twitter_api_key', 'twitter_api_secret_key', 
                    'twitter_access_token', 'twitter_access_token_secret',
                    'twitter_bearer_token')
    def validate_twitter_credentials(cls, v: Optional[str], info) -> Optional[str]:
        """Validate Twitter credentials."""
        if v is None:
            return None
            
        v = v.strip()
        if v.lower() in ['your-twitter-key', 'your-api-key', 'your-token']:
            return None
            
        if len(v) < 20:  # Most Twitter keys are longer
            return None
            
        return v

    def get(self, key: str, default=None):
        """Dict-like accessor. WARNING: keys that are not declared fields fall back to
        `default` and never read the environment — declare feature flags as fields."""
        if not hasattr(self, key):
            logging.getLogger(__name__).debug(
                "BotConfig.get('%s') is not a declared field; returning default %r "
                "(env var, if any, is NOT read)", key, default
            )
        return getattr(self, key, default)

    def _load_api_keys(self) -> None:
        """Load API keys from config."""
        # Twitter API keys
        twitter = {k: v for k, v in self.get_twitter_config().items() if v}
        self.twitter_api_key = twitter.get('api_key')
        self.twitter_api_secret_key = twitter.get('api_secret')
        self.twitter_access_token = twitter.get('access_token')
        self.twitter_access_token_secret = twitter.get('access_token_secret')
        self.twitter_bearer_token = twitter.get('bearer_token')

    def get_llm_config(self) -> Dict[str, Any]:
        """Get LLM configuration - API keys and endpoints only.

        Model selection is handled by llm_client_registry.DEFAULT_MODELS.
        This method only provides connection credentials.
        """
        return {
            "openai": {
                "api_key": self.openai_api_key,
            },
            "anthropic": {
                "api_key": self.anthropic_api_key,
            },
            "deepseek": {
                "api_key": self.deepseek_api_key,
                "api_url": self.deepseek_api_url,
            },
            "gemini": {
                "api_key": self.gemini_api_key,
            },
            "openrouter": {
                "api_key": self.openrouter_api_key,
                "site_url": self.openrouter_site_url,
                "site_name": self.openrouter_site_name,
            },
            "nvidia": {
                "api_key": self.nvidia_api_key,
                "api_url": self.nvidia_api_url,
            }
        }

    # REMOVED: get_model_config() and validate_model_name() 
    # Use modules.llm.model_registry.get_model_config() instead

    # Optional override for telemetry config
    telemetry_config: Optional[Dict[str, Any]] = None

    # Browser settings
    browser: dict = {
        'headless': False,
        'timeout': 30000,
        'viewport': {
            'width': 1280,
            'height': 720
        }
    }
    
    # Agent settings
    agent: dict = {
        'max_steps': 50,
        'retry_delay': 2,
        'max_failures': 3
    }
    
    # Controller settings
    controller: dict = {
        'loop_threshold': 3,
        'max_url_visits': 2
    }

    # REMOVED: model_configs dict - use modules.llm.model_registry instead
    # All model configurations (context windows, max tokens, pricing) are in model_registry.py

    def get_twitter_config(self) -> Dict[str, str]:
        """Get Twitter API configuration."""
        twitter_config = {
            'api_key': self.twitter_api_key,
            'api_secret': self.twitter_api_secret_key,  # Match the field name
            'access_token': self.twitter_access_token,
            'access_token_secret': self.twitter_access_token_secret,
            'bearer_token': self.twitter_bearer_token
        }
        # Only return values that are actually set
        return {k: v for k, v in twitter_config.items() if v and v.strip()}

    # Collab.Land Configuration
    collabland_api_key: str = Field("", alias='COLLABLAND_API_KEY')
    collabland_api_url: str = Field("https://api.collab.land", alias='COLLABLAND_API_URL')
    collabland_rules: List[Dict[str, Any]] = Field(default_factory=list)
    
    # Update with full credentials
    collabland_id: str = Field("", alias='COLLABLAND_ID') 
    collabland_secret: str = Field("", alias='COLLABLAND_SECRET')
    
    # Alchemy API Configuration
    alchemy_api_key: str = Field("", alias='ALCHEMY_API_KEY')
    alchemy_api_url: str = Field("https://eth-mainnet.g.alchemy.com", alias='ALCHEMY_API_URL')
    
    # Den token configuration
    den_token_contract_address: str = Field("", alias='DEN_TOKEN_CONTRACT_ADDRESS')

    # ============================================
    # AUTHENTICATION & CREDIT SYSTEM (FREE - No Privy!)
    # ============================================

    # REMOVED: Privy ($599/month) - replaced with FREE SIWE
    # privy_app_id: Deleted - not needed anymore!
    # privy_app_secret: Deleted - not needed anymore!

    # Wallet Generation (for deposit addresses)
    master_seed: Optional[str] = Field(None, alias='MASTER_SEED', description="Master seed for deterministic wallet generation - KEEP SECRET!")

    # Blockchain RPC URLs
    ethereum_rpc_url: Optional[str] = Field(None, alias='ETHEREUM_RPC_URL', description="Ethereum Mainnet RPC URL (e.g., Alchemy)")
    sepolia_rpc_url: Optional[str] = Field(None, alias='SEPOLIA_RPC_URL', description="Sepolia Testnet RPC URL (e.g., Alchemy)")
    polygon_rpc_url: Optional[str] = Field(None, alias='POLYGON_RPC_URL', description="Polygon RPC URL (e.g., Alchemy)")
    base_rpc_url: Optional[str] = Field(None, alias='BASE_RPC_URL', description="Base RPC URL")
    arbitrum_rpc_url: Optional[str] = Field(None, alias='ARBITRUM_RPC_URL', description="Arbitrum RPC URL")

    # Treasury
    treasury_address: Optional[str] = Field(None, alias='TREASURY_ADDRESS', description="Gnosis Safe or treasury wallet address")

    # System Flags
    enable_auth: bool = Field(False, alias='ENABLE_AUTH', description="Enable authentication system")
    enable_credit_system: bool = Field(True, alias='ENABLE_CREDIT_SYSTEM', description="Enable credit tracking and metering")
    deposit_monitor_enabled: bool = Field(False, alias='DEPOSIT_MONITOR_ENABLED', description="Enable deposit monitoring service")
    deposit_check_interval: int = Field(60, alias='DEPOSIT_CHECK_INTERVAL', description="Deposit check interval in seconds")
    sweep_interval: int = Field(3600, alias='SWEEP_INTERVAL', description="Treasury sweep interval in seconds (default: 1 hour)")
    min_sweep_usd: float = Field(50.0, alias='MIN_SWEEP_USD', description="Minimum USD amount to trigger sweep")

    # JWT Configuration
    jwt_secret_key: Optional[str] = Field(None, alias='JWT_SECRET_KEY', description="Secret key for JWT signing")

    # Beta Access Control
    beta_mode_enabled: bool = Field(True, alias='BETA_MODE_ENABLED', description="Enable beta mode access restrictions")
    require_den_token: bool = Field(True, alias='REQUIRE_DEN_TOKEN', description="Require DEN token ownership for access")
    bypass_den_check_for_admins: bool = Field(True, alias='BYPASS_DEN_CHECK_FOR_ADMINS', description="Allow admins to bypass DEN token check")
    bypass_payment_for_admins: bool = Field(True, alias='BYPASS_PAYMENT_FOR_ADMINS', description="Allow admins to bypass payment checks")

    # x402 Protocol Configuration
    x402_enabled: bool = Field(False, alias='X402_ENABLED')
    x402_facilitator_url: str = Field(
        "",  # Empty = use direct signature verification (no external facilitator)
        alias='X402_FACILITATOR_URL'
    )
    x402_facilitator_api_key: Optional[str] = Field(None, alias='X402_FACILITATOR_API_KEY')
    x402_facilitator_api_secret: Optional[str] = Field(None, alias='X402_FACILITATOR_API_SECRET')
    x402_default_chain: str = Field("base", alias='X402_DEFAULT_CHAIN')
    x402_payment_recipient: Optional[str] = Field(None, alias='X402_PAYMENT_RECIPIENT')
    x402_payment_deadline_seconds: int = Field(300, alias='X402_PAYMENT_DEADLINE')

    # Agent personal wallet (core; distinct from x402 receive/tariffing gateway)
    agent_wallet_enabled: bool = Field(False, alias='AGENT_WALLET_ENABLED')
    agent_wallet_backend: str = Field("local_eoa", alias='AGENT_WALLET_BACKEND')
    agent_wallet_network: str = Field("testnet", alias='AGENT_WALLET_NETWORK')
    agent_wallet_max_per_tx_usd: float = Field(1000.0, alias='AGENT_WALLET_MAX_PER_TX_USD')
    x402_client_enabled: bool = Field(False, alias='X402_CLIENT_ENABLED')
    x402_client_facilitator_url: str = Field("", alias='X402_CLIENT_FACILITATOR_URL')
    # NOTE: AGENT_WALLET_MASTER_SEED is read directly by core/wallet/config.py and is
    # intentionally NOT mirrored here (keep the secret out of the broad BotConfig surface).

    # ERC-8004 Trustless Agents Configuration
    # https://eips.ethereum.org/EIPS/eip-8004
    eip8004_enabled: bool = Field(False, alias='EIP8004_ENABLED', description="Enable ERC-8004 Trustless Agents integration")
    eip8004_chain_id: int = Field(8453, alias='EIP8004_CHAIN_ID', description="Chain ID for ERC-8004 registries (default: Base)")
    eip8004_identity_registry: Optional[str] = Field(None, alias='EIP8004_IDENTITY_REGISTRY', description="Identity Registry contract address")
    eip8004_reputation_registry: Optional[str] = Field(None, alias='EIP8004_REPUTATION_REGISTRY', description="Reputation Registry contract address")
    eip8004_validation_registry: Optional[str] = Field(None, alias='EIP8004_VALIDATION_REGISTRY', description="Validation Registry contract address")
    eip8004_agent_id: Optional[int] = Field(None, alias='EIP8004_AGENT_ID', description="On-chain agent ID (ERC-721 tokenId)")
    eip8004_agent_wallet: Optional[str] = Field(None, alias='EIP8004_AGENT_WALLET', description="Agent wallet for signing")
    eip8004_agent_private_key: Optional[str] = Field(None, alias='EIP8004_AGENT_PRIVATE_KEY', description="Agent private key for EIP-712 signatures - KEEP SECRET!")
    eip8004_supported_trust: str = Field("reputation", alias='EIP8004_SUPPORTED_TRUST', description="Comma-separated trust models: reputation,crypto-economic,tee-attestation")


# Back-compat: existing `from core.config import BotConfig` call sites keep working.
# BotConfig is the platform config; it will live in polyrob-platform after the split.
BotConfig = ServerConfig
