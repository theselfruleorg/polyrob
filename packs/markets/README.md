# The markets pack

A first-party POLYROB pack for prediction markets and perpetuals:

- `polymarket` and `hyperliquid` — the trade tools. Every order, cancel,
  leverage and agent-delegation verb is on the owner approval lane and blocked
  while a turn is correspondent-tainted. Live submission is OFF unless
  `CRYPTO_TRADE_LIVE_ENABLED` and the venue switch (`POLYMARKET_TRADING_ENABLED`,
  `HYPERLIQUID_TRADING_ENABLED`) are on; otherwise the tools dry-run.
- `polymarket_data` and `hyperliquid_data` — read-only market data (no wallet,
  no signing, delegable).
- the credential and audit stores (the `polymarket_*` / `hyperliquid_*` tables
  in the core database; the tables self-create and are never dropped);
- the API routes, mounted at `/api/packs/markets/polymarket/*` and
  `/api/packs/markets/hyperliquid/*`;
- six skills: `polymarket-market-research`, `polymarket-portfolio-review`,
  `polymarket-trading`, `hyperliquid-market-data`, `hyperliquid-account-review`,
  `hyperliquid-trading`. The `crypto-trading-safety` skill stays in core (it
  also covers the on-chain treasury rail).

It ships inside the `polyrob` distribution: there is nothing separate to
install. Its SDKs (hyperliquid-python-sdk, py-clob-client-v2 (and the wallet)) are the `crypto` extra:

```bash
pip install 'polyrob[crypto]'
polyrob pack list                  # markets ... loaded
```

Without the extra the pack still loads; the `polymarket` and `hyperliquid` trade tools is withheld and
`polyrob pack list` names the remedy (`needs pip install 'polyrob[crypto]'`).
From a source tree, `install.sh --packs markets` installs the extra hash-checked.

Configuration flags (`POLYMARKET_*`, `HYPERLIQUID_*`, `CRYPTO_TRADE_*`) are
documented in the core `docs/CONFIGURATION.md`.
