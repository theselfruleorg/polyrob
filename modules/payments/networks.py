"""Shared chain/token tables for the payments services.

ONE home for the chain configs, stablecoin contract addresses, and minimal
ERC-20 ABI fragments used by both ``DepositMonitor`` and ``TreasurySweeper``.
They used to carry verbatim copies of all three — a one-sided address edit
would silently misroute real funds. (``core/wallet/onchain.py`` is a separate,
Base/Arbitrum/Polygon table for the agent wallet; this one is the
ethereum/sepolia deposit rail.)
"""

from typing import Any, Dict


def chain_configs(config: Any) -> Dict[str, Dict[str, Any]]:
    """Chain configs keyed by chain name, RPC urls sourced from BotConfig."""
    return {
        'ethereum': {
            'rpc_url': getattr(config, 'ethereum_rpc_url', None),
            'chain_id': 1
        },
        'sepolia': {
            'rpc_url': getattr(config, 'sepolia_rpc_url', None),
            'chain_id': 11155111
        }
    }


# Token contract addresses (stablecoins, 6 decimals)
TOKEN_ADDRESSES: Dict[str, Dict[str, str]] = {
    'ethereum': {
        'USDC': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
        'USDT': '0xdAC17F958D2ee523a2206206994597C13D831ec7'
    },
    'sepolia': {
        'USDC': '0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238',  # Sepolia USDC
        'USDT': '0x7169D38820dfd117C3FA1f22a697dBA58d90BA06'   # Sepolia USDT (example)
    }
}

ERC20_BALANCEOF_ABI = [{
    "constant": True,
    "inputs": [{"name": "_owner", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "balance", "type": "uint256"}],
    "type": "function"
}]

ERC20_TRANSFER_ABI = ERC20_BALANCEOF_ABI + [{
    "constant": False,
    "inputs": [
        {"name": "_to", "type": "address"},
        {"name": "_value", "type": "uint256"}
    ],
    "name": "transfer",
    "outputs": [{"name": "", "type": "bool"}],
    "type": "function"
}]
