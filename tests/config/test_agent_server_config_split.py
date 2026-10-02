import pytest
from core.config import AgentConfig, ServerConfig, BotConfig

# The exact platform fields that must live on ServerConfig, NOT AgentConfig.
PLATFORM_FIELDS = [
    "enable_auth", "jwt_secret_key", "bypass_payment_for_admins",
    "enable_credit_system", "deposit_monitor_enabled", "deposit_check_interval",
    "sweep_interval", "min_sweep_usd",
    "master_seed", "ethereum_rpc_url", "sepolia_rpc_url", "polygon_rpc_url",
    "base_rpc_url", "arbitrum_rpc_url", "treasury_address",
    "x402_enabled", "x402_facilitator_url",
    "collabland_api_key", "collabland_api_url", "collabland_rules", "collabland_id",
    "collabland_secret",
    "alchemy_api_key", "alchemy_api_url", "den_token_contract_address",
]
# 067 F4 deleted the unread x402/agent-wallet/ERC-8004/beta-gate mirrors; those
# env names are read directly by core/wallet/config.py and modules/eip8004/.


def test_serverconfig_subclasses_agentconfig():
    assert issubclass(ServerConfig, AgentConfig)


def test_botconfig_is_serverconfig_alias():
    assert BotConfig is ServerConfig


@pytest.mark.parametrize("field", PLATFORM_FIELDS)
def test_platform_field_not_on_agentconfig(field):
    assert field not in AgentConfig.model_fields, (
        f"{field} is a platform field and must not be declared on AgentConfig"
    )


@pytest.mark.parametrize("field", PLATFORM_FIELDS)
def test_platform_field_on_serverconfig(field):
    assert field in ServerConfig.model_fields, f"{field} must be declared on ServerConfig"


def test_agentconfig_constructs_without_platform_env(monkeypatch):
    # Clearing platform env must not break a bare AgentConfig.
    for env in ("ENABLE_AUTH", "TREASURY_ADDRESS", "X402_ENABLED", "JWT_SECRET_KEY"):
        monkeypatch.delenv(env, raising=False)
    cfg = AgentConfig()
    assert not hasattr(cfg, "treasury_address")
