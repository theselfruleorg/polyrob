from core.wallet.config import load_wallet_config, TESTNET_FACILITATOR_URL


def test_defaults_are_safe():
    cfg = load_wallet_config({})
    assert cfg.enabled is False
    assert cfg.x402_client_enabled is False
    assert cfg.network == "testnet"
    assert cfg.backend == "local_eoa"
    assert cfg.master_seed is None
    assert cfg.x402_facilitator_url == TESTNET_FACILITATOR_URL
    assert cfg.daily_cap_usd is None  # disabled by default = legacy behavior


def test_daily_cap_parsed_when_set():
    assert load_wallet_config({"WALLET_DAILY_CAP_USD": "5"}).daily_cap_usd == 5.0
    # blank / unparseable → disabled (None), never a crash
    assert load_wallet_config({"WALLET_DAILY_CAP_USD": ""}).daily_cap_usd is None
    assert load_wallet_config({"WALLET_DAILY_CAP_USD": "abc"}).daily_cap_usd is None


def test_reads_env():
    env = {
        "AGENT_WALLET_ENABLED": "true",
        "AGENT_WALLET_MASTER_SEED": "x" * 40,
        "AGENT_WALLET_NETWORK": "mainnet",
        "AGENT_WALLET_MAX_PER_TX_USD": "250",
        "X402_CLIENT_ENABLED": "true",
        "X402_CLIENT_FACILITATOR_URL": "https://facilitator.example",
    }
    cfg = load_wallet_config(env)
    assert cfg.enabled is True
    assert cfg.network == "mainnet"
    assert cfg.max_per_tx_usd == 250.0
    assert cfg.x402_client_enabled is True
    assert cfg.x402_facilitator_url == "https://facilitator.example"
