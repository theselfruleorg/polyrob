"""Income is denominated on known production chains, never test networks."""
from core.wallet import chains


def valued_chain_names() -> tuple[str, ...]:
    names = []
    for row in chains.all_rows():
        if not row.valueless:
            names.append(row.name)
            if row.family == "evm":
                names.append(f"eip155:{row.chain_id}")
    return tuple(names)


#: Test-network assets that share a production chain NAME. Solana devnet USDC
#: is stored as chain ``solana`` (the registry has one Solana row), so the
#: chain alone cannot tell a devnet invoice from a mainnet one.
TEST_ASSET_IDS = ("usdc-solana-devnet",)


def is_test_asset(asset_id) -> bool:
    a = str(asset_id or "").strip().lower()
    return a in TEST_ASSET_IDS or a.endswith(("-devnet", "-testnet", "-sepolia"))


def is_income_chain(chain, asset_id=None) -> bool:
    # Legacy invoices without a chain were Base invoices.
    return (str(chain or "base").lower() in valued_chain_names()
            and not is_test_asset(asset_id))


def income_predicate() -> tuple[str, tuple[str, ...]]:
    names = valued_chain_names()
    slots = ",".join("?" for _ in names)
    tests = ",".join("?" for _ in TEST_ASSET_IDS)
    return (f"(LOWER(COALESCE(NULLIF(chain, ''), 'base')) IN ({slots}) "
            f"AND LOWER(COALESCE(asset_id, '')) NOT IN ({tests}) "
            "AND LOWER(COALESCE(asset_id, '')) NOT LIKE '%-devnet' "
            "AND LOWER(COALESCE(asset_id, '')) NOT LIKE '%-testnet' "
            "AND LOWER(COALESCE(asset_id, '')) NOT LIKE '%-sepolia')",
            names + TEST_ASSET_IDS)
