"""`polyrob wallet` — show the agent wallet: per-venue addresses, on-chain
balances, network, caps, and which venue is OPERATIONAL (the one funded + spent from).

This closes the interface gap that produced the 2026-07-08 fund-the-wrong-address
incident: the owner had no single place to see "what's my address / balance / which
one do I fund". Balances are best-effort over public RPCs (fail-open to n/a).
"""
from __future__ import annotations

import json as _json

import click

# venue → chain it settles on (mainnet). Same EOA address on every EVM chain.
_VENUE_CHAIN = {
    "treasury": "base",
    "x402": "base",
    "hyperliquid": "arbitrum",
    "polymarket": "polygon",
}
# chain → (rpc, usdc erc20 contract, native symbol)
_CHAIN = {
    "base": ("https://mainnet.base.org", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "ETH"),
    "arbitrum": ("https://arb1.arbitrum.io/rpc", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "ETH"),
    "polygon": ("https://polygon-rpc.com", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "POL"),
}
_ERC20_BALANCEOF = "0x70a08231000000000000000000000000"  # balanceOf(address) selector + pad


# Only these venues hold a same-chain float the agent spends directly. hyperliquid
# (delegated signer, collateral in the master account) and polymarket (per-user proxy
# creds) NEVER hold funds at their derived address — showing a fundable balance there
# would re-create the fund-the-wrong-address footgun this whole change exists to kill.
_FUNDABLE = {"treasury", "x402"}


def _rpc(url: str, method: str, params: list, timeout: float = 4.0):
    import urllib.request
    body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec - public read-only RPC
        return _json.loads(r.read()).get("result")


def _balances(address: str, chain: str):
    """(native, usdc) as floats, or (None, None) on any failure. Best-effort."""
    cfg = _CHAIN.get(chain)
    if not cfg:
        return None, None
    rpc, usdc, _sym = cfg
    native = usdc_bal = None

    def _hex_int(raw):  # "0x"/None/"" → 0, else parse
        return int(raw, 16) if raw and raw != "0x" else 0
    try:
        native = _hex_int(_rpc(rpc, "eth_getBalance", [address, "latest"])) / 1e18
    except Exception:
        pass
    try:
        data = _ERC20_BALANCEOF + address[2:].lower()
        usdc_bal = _hex_int(_rpc(rpc, "eth_call", [{"to": usdc, "data": data}, "latest"])) / 1e6
    except Exception:
        pass
    return native, usdc_bal


@click.command("wallet")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--no-balances", is_flag=True, help="Skip the on-chain balance lookups (offline/fast).")
def wallet_cmd(as_json: bool, no_balances: bool):
    """Show the agent wallet: addresses, balances, network, caps, operational venue."""
    from core.wallet.factory import get_agent_wallet
    w = get_agent_wallet()
    if w is None:
        msg = "agent wallet not enabled (set AGENT_WALLET_ENABLED=true)"
        click.echo(_json.dumps({"enabled": False, "error": msg}) if as_json else msg)
        return

    cfg = w.config
    op = w.operational_venue
    venues = []
    for venue in ("treasury", "x402", "hyperliquid", "polymarket"):
        addr = w.signer_for(venue).address
        chain = _VENUE_CHAIN[venue]
        fundable = venue in _FUNDABLE
        row = {"venue": venue, "address": addr, "chain": chain,
               "operational": venue == op, "fundable": fundable}
        if not fundable:
            # delegated/managed elsewhere — never fund this derived address directly
            row["note"] = "delegated signer — not funded here"
        if fundable and not no_balances and cfg.network == "mainnet":
            native, usdc = _balances(addr, chain)
            row["usdc"] = usdc
            row["native"] = native
        venues.append(row)

    caps = {
        "max_per_tx_usd": cfg.max_per_tx_usd,
        "daily_cap_usd": cfg.daily_cap_usd,
        "per_venue_daily_cap_usd": cfg.per_venue_daily_cap_usd,
    }
    payload = {"enabled": True, "network": cfg.network, "operational_venue": op,
               "address": w.address, "venues": venues, "caps": caps}

    if as_json:
        click.echo(_json.dumps(payload, indent=2))
        return

    click.echo(f"Agent wallet · network={cfg.network} · operational venue={op}")
    click.echo(f"Fund THIS address (operational): {w.address}")
    if cfg.network != "mainnet":
        click.echo(click.style("  ⚠ network is not mainnet — on-chain balances not shown.", fg="yellow"))
    click.echo("")
    for r in venues:
        star = " ←FUND" if r["operational"] else ""
        line = f"  {r['venue']:11s} {r['address']}  [{r['chain']}]{star}"
        if "usdc" in r:
            u = f"{r['usdc']:.2f}" if r["usdc"] is not None else "n/a"
            n = f"{r['native']:.5f}" if r["native"] is not None else "n/a"
            line += f"   USDC={u} gas={n}"
        elif r.get("note"):
            line += click.style(f"   ({r['note']})", fg="yellow")
        click.echo(line)
    click.echo("")
    dc = f"${caps['daily_cap_usd']:.2f}" if caps["daily_cap_usd"] is not None else "none"
    click.echo(f"Caps: max ${caps['max_per_tx_usd']:.2f}/tx · daily {dc}"
               + (f" · per-venue {caps['per_venue_daily_cap_usd']}" if caps["per_venue_daily_cap_usd"] else ""))
