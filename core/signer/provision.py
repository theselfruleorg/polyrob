"""Generate ``/etc/polyrob/signer.toml`` from the configuration IN USE (066 D6).

``python -I -m core.signer.provision --envfile /etc/polyrob/polyrob.env
--data-dir /var/lib/polyrob --agent-uid 998`` prints the TOML on stdout (the
provisioning script writes it root-owned). Never reads ``wallet.env``, never
prints a secret: hard caps must be explicit positive values in the operator env
file. Agent-writable preferences cannot influence the independent signer limits.
Addresses come from ``<data>/wallet/public_identity.json`` and RPC pins from env.
"""
import argparse
import json
import os
import sys
from typing import Dict, List, Mapping, Optional

from core.signer.caps import SignerConfig, render_signer_toml
from core.signer import DEFAULT_SOCKET, DEFAULT_STATE_DIR

_RPC_PREFIX = "DEFI_EVM_RPC_"


class ProvisionError(RuntimeError):
    pass


def parse_env_file(path: str) -> Dict[str, str]:
    """The env file as the units see it (parse parity with ``core.bootstrap.load_env``)."""
    from dotenv import dotenv_values
    if not os.path.isfile(path):
        raise ProvisionError(f"{path} does not exist")
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def current_caps(env: Mapping[str, str]) -> Dict[str, float]:
    """Hard caps come only from explicit operator env values, never agent prefs."""
    from core.wallet.config import explicit_operator_caps
    try:
        return explicit_operator_caps(env)
    except ValueError as exc:
        raise ProvisionError(str(exc)) from exc


def money_chains_with_pins(env: Mapping[str, str]) -> List[str]:
    from core.wallet import chains
    out = []
    for name in chains.money_chains():
        row = chains.get(name)
        if row is not None and str(env.get(row.rpc_env, "")).strip():
            out.append(name)
    return out


def identity_record(data_dir: str) -> Dict[str, str]:
    path = os.path.join(data_dir, "wallet", "public_identity.json")
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        raise ProvisionError(f"{path} does not exist — start the agent once (it publishes "
                             f"its addresses) before provisioning the signer")
    out = {k: v for k, v in (record.get("evm") or {}).items() if v}
    if record.get("solana"):
        out["solana"] = record["solana"]
    if "treasury" not in out:
        raise ProvisionError(f"{path} records no treasury address")
    return out


def build_config(env: Mapping[str, str], *, data_dir: str, agent_uids: List[int],
                 socket_group: Optional[str] = "polyrob-signer-clients") -> SignerConfig:
    caps = current_caps(env)
    chains = money_chains_with_pins(env)
    pins = {k: v for k, v in env.items()
            if (k.startswith(_RPC_PREFIX) or k in ("ALCHEMY_API_KEY", "SOLANA_RPC_URL")) and v}
    network = str(env.get("AGENT_WALLET_NETWORK", "testnet")).strip().lower() or "testnet"
    venue = str(env.get("AGENT_WALLET_OPERATIONAL_VENUE", "treasury")).strip().lower() or "treasury"
    dest = str(env.get("TREASURY_ADDRESS", "")).strip() or None
    # The deposit token the sweeper moves is USDC; its address per chain is the
    # chain registry's PINNED one (never an env value).
    tokens: Dict[str, str] = {}
    if dest:
        from core.wallet import chains as _chains
        for name in chains:
            row = _chains.get(name)
            if row is not None and row.usdc:
                tokens[f"{name}:USDC"] = str(row.usdc)
    return SignerConfig(
        per_tx_usd=caps["per_tx_usd"], daily_usd=caps["daily_usd"],
        x402_per_payment_usd=caps["per_tx_usd"], chains=tuple(chains),
        socket=DEFAULT_SOCKET, state_dir=DEFAULT_STATE_DIR,
        client_uids=tuple(int(u) for u in agent_uids), socket_group=socket_group,
        network=network if network in ("testnet", "mainnet") else "testnet",
        operational_venue=venue if venue in ("treasury", "x402") else "treasury",
        expected_addresses=identity_record(data_dir),
        sweep_destination=dest if dest and dest.startswith("0x") and len(dest) == 42 else None,
        sweep_tokens=tokens, env=pins)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m core.signer.provision")
    p.add_argument("--envfile", default="/etc/polyrob/polyrob.env")
    p.add_argument("--data-dir", default="/var/lib/polyrob")
    p.add_argument("--agent-uid", type=int, action="append", required=True)
    args = p.parse_args(argv)
    env = parse_env_file(args.envfile)
    # Resolve the deployed identity location. Hard caps never read prefs.
    os.environ["POLYROB_DATA_DIR"] = args.data_dir
    try:
        cfg = build_config(env, data_dir=args.data_dir, agent_uids=args.agent_uid)
    except ProvisionError as exc:
        print(f"provision: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(render_signer_toml(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
