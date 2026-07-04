"""PR 3 config-split and phase-6-conditional locks."""

import inspect

import pytest


# --- Cut A: config split ----------------------------------------------------


def test_agent_config_exists_and_has_agent_fields():
    from core.config import AgentConfig

    ac = AgentConfig()
    # LLM keys
    for field in [
        'openai_api_key',
        'anthropic_api_key',
        'gemini_api_key',
        'deepseek_api_key',
        'openrouter_api_key',
        'nvidia_api_key',
        'perplexity_api_key',
    ]:
        assert hasattr(ac, field), f"AgentConfig missing {field}"

    # Sessions / paths / persona
    for field in [
        'session_ttl_seconds',
        'max_sessions_in_memory',
        'session_cleanup_interval',
        'max_sessions_per_user',
        'default_character',
        'data_dir',
        'db_path',
        'log_level',
        'sub_agents_enabled',
        'max_sub_agent_depth',
    ]:
        assert hasattr(ac, field), f"AgentConfig missing {field}"


def test_agent_config_has_no_server_fields():
    """AgentConfig is the core-scope surface — it must not expose server-only fields."""
    from core.config import AgentConfig

    ac = AgentConfig()
    server_only = [
        'jwt_secret_key',
        'x402_enabled',
        'x402_payment_recipient',
        'enable_auth',
        'enable_credit_system',
        'master_seed',
        'admin_ids',
        'moderator_ids',
        'treasury_address',
        'ethereum_rpc_url',
        'eip8004_enabled',
        'twitter_api_key',
        'gmail_email',
        'alchemy_api_key',
    ]
    leaked = [f for f in server_only if hasattr(ac, f)]
    assert not leaked, f"AgentConfig leaks server-scope fields: {leaked}"


def test_bot_config_extends_agent_config(monkeypatch):
    """BotConfig inherits everything from AgentConfig (backward compat)."""
    from core.config import AgentConfig, BotConfig

    # This asserts the DEFAULT session_ttl; BotConfig reads SESSION_TTL_SECONDS
    # from the process env, which other tests (load_env) can leak (config/.env.*
    # sets 3600). Clear it so the default is tested deterministically regardless
    # of suite ordering.
    monkeypatch.delenv("SESSION_TTL_SECONDS", raising=False)

    assert issubclass(BotConfig, AgentConfig)
    bc = BotConfig()
    # Agent fields still accessible via the subclass
    assert hasattr(bc, 'openai_api_key')
    assert hasattr(bc, 'session_ttl_seconds')
    assert bc.session_ttl_seconds == 86400
    assert bc.default_character == 'rob'


def test_bot_config_keeps_server_fields():
    """Existing call sites that use BotConfig server fields must still work."""
    from core.config import BotConfig

    bc = BotConfig()
    for field in [
        'jwt_secret_key',
        'x402_enabled',
        'enable_auth',
        'admin_ids',
        'master_seed',
    ]:
        assert hasattr(bc, field), f"BotConfig lost server field {field}"


def test_session_ttl_validator_inherited():
    """The agent-side validator must run when AgentConfig is mutated."""
    from core.config import AgentConfig

    ac = AgentConfig()
    with pytest.raises(Exception, match="60 seconds"):
        ac.session_ttl_seconds = 5  # below minimum, validate_assignment triggers


# --- Cut B: phase-6 conditional ---------------------------------------------


def test_core_bot_initialize_does_not_call_phase_6():
    """CoreBot._initialize must run phases 1-5 only.

    Source-text check because the function is heavily decorated and asyncio.
    """
    with open('core/bot.py') as f:
        src = f.read()

    # Find the _initialize method body
    import re
    m = re.search(r'async def _initialize\(self\)(.*?)(?=async def )', src, re.DOTALL)
    assert m, "_initialize method not found in core/bot.py"
    body = m.group(1)
    assert 'initialize_auth_services' not in body, (
        "CoreBot._initialize must not auto-call phase 6 auth services; "
        "use build_server_bot from core.bootstrap instead"
    )


def test_core_bot_exposes_initialize_server_services():
    """Phase 6 is opt-in via an explicit method on Bot."""
    from core.bot import Bot

    assert hasattr(Bot, 'initialize_server_services')
    method = Bot.initialize_server_services
    assert inspect.iscoroutinefunction(method)


def test_build_server_bot_exists():
    """build_server_bot is the server entry point that adds phase 6."""
    from core.bootstrap import build_bot, build_server_bot

    assert build_server_bot is not build_bot
    assert inspect.iscoroutinefunction(build_server_bot)


def test_api_app_uses_build_server_bot():
    """api/app.py wires the server entry point, not the plain core bootstrap."""
    with open('api/app.py') as f:
        src = f.read()
    assert 'build_server_bot' in src
    assert 'await build_server_bot()' in src


# --- Cut C: permissions import-clean ---------------------------------------


def test_core_permissions_does_not_eagerly_import_modules_memory():
    """core/permissions.py must keep the MemoryManager import behind TYPE_CHECKING.

    Source-text check is more robust than a runtime import test because the
    transitive import graph is large.
    """
    with open('core/permissions.py') as f:
        src = f.read()

    # Find the line that imports MemoryManager
    import re
    matches = [
        (i, line)
        for i, line in enumerate(src.split('\n'))
        if 'from modules.memory.memory_manager' in line
    ]
    assert matches, "MemoryManager import line not found (expected at least one)"
    # All MemoryManager imports must live inside the TYPE_CHECKING block
    # (which is the first block after the module imports). Verify by scanning
    # whether 'if TYPE_CHECKING' appears before each match.
    pre_typecheck_imports = []
    typecheck_seen = False
    for line in src.split('\n'):
        if 'if TYPE_CHECKING' in line:
            typecheck_seen = True
            continue
        if 'from modules.memory.memory_manager' in line and not typecheck_seen:
            pre_typecheck_imports.append(line)
    assert not pre_typecheck_imports, (
        "MemoryManager must only be imported inside TYPE_CHECKING; "
        f"found eager imports: {pre_typecheck_imports}"
    )
