"""``/etc/polyrob/signer.toml`` — the policy the agent cannot change (066 §5.3).

Root writes it (``deployment/hardening/install-signer.sh`` generates it from the
caps IN USE — owner decision D6: keep current values, never new numbers);
``polyrob-signer`` reads it at start. The agent-side caps
(``AGENT_WALLET_MAX_PER_TX_USD``, ``WALLET_DAILY_CAP_USD``, the owner's
``budget.*`` prefs) keep working BELOW these; a request above them needs an
owner approval from the CLI on the box (D5).

Strict: an unknown table or key refuses to load, a missing or non-finite cap
refuses to load. A signer that starts on a half-read policy is worse than one
that does not start.

Layout::

    [caps]
    per_tx_usd = 250.0          # hard per-transaction cap
    daily_usd = 100.0           # hard rolling-24h cap (the signer's own ledger)
    x402_per_payment_usd = 250.0   # optional, default per_tx_usd

    [policy]
    chains = ["base"]           # EVM chains the signer will sign for
    approval_ttl_sec = 3600
    x402_max_window_sec = 600
    hyperliquid_orders = false  # 066 P3: sign Hyperliquid L1 (order) actions

    [server]
    socket = "/run/polyrob-signer/signer.sock"
    socket_group = "polyrob-signer-clients"
    state_dir = "/var/lib/polyrob-signer"
    client_uids = [998]         # the agent UID(s); root (0) is always the owner
    peer_check = "warn"         # uid | warn | main_process (core/signer/peer.py)

    [identity]                  # the addresses of record (wallet continuity)
    network = "mainnet"
    operational_venue = "treasury"
    treasury = "0x..."
    solana = "..."

    [sweep]                     # optional: the deposit sweeper (066 §5.5)
    destination = "0x..."
    tokens = { "base:USDC" = "0x..." }

    [env]                       # RPC pins for the signer's OWN reads
    DEFI_EVM_RPC_BASE = "https://..."


069 v4 retired the ``[account_binding]`` table (and its pre-069 name ``[desk]``): the
signer keeps no account binding. A file that still carries one loads, with a logged
warning, and the table is ignored — it only ever tightened signing.
"""
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_TABLES = {
    "caps": {"per_tx_usd", "daily_usd", "x402_per_payment_usd"},
    "policy": {"chains", "approval_ttl_sec", "x402_max_window_sec", "hyperliquid_orders"},
    "server": {"socket", "socket_group", "state_dir", "client_uids", "peer_check"},
    "identity": {"network", "operational_venue", "treasury", "x402", "polymarket",
                 "hyperliquid", "solana"},
    "sweep": {"destination", "tokens"},
    "env": None,   # keys checked against _ENV_NAMES
}
#: Tables 069 v4 retired (the account binding). Ignored with a warning, never read, so a
#: signer.toml written before the simple model still starts.
_RETIRED_TABLES = ("account_binding", "desk")
#: The only env names ``[env]`` may set in the signer process. An RPC pin (the
#: guard refuses to arm on a shared public endpoint) and the Alchemy key the RPC
#: resolver composes an endpoint from. Never a flag that loosens a guard.
_ENV_NAMES = re.compile(r"^(DEFI_EVM_RPC_[A-Z0-9_]+|ALCHEMY_API_KEY|SOLANA_RPC_URL)$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class SignerConfigError(ValueError):
    """``signer.toml`` is missing, unreadable or does not match the schema."""


@dataclass(frozen=True)
class SignerConfig:
    per_tx_usd: float
    daily_usd: float
    x402_per_payment_usd: float
    chains: Tuple[str, ...]
    socket: str
    state_dir: str
    client_uids: Tuple[int, ...]
    socket_group: Optional[str] = None
    peer_check: str = "warn"
    approval_ttl_sec: int = 3600
    x402_max_window_sec: int = 600
    hyperliquid_orders: bool = False
    network: str = "mainnet"
    operational_venue: str = "treasury"
    expected_addresses: Dict[str, str] = field(default_factory=dict)
    sweep_destination: Optional[str] = None
    sweep_tokens: Dict[str, str] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict, repr=False)

    def summary(self) -> Dict[str, object]:
        """What ``ping`` reports: the caps, never the env (RPC URLs carry keys)."""
        return {"per_tx_usd": self.per_tx_usd, "daily_usd": self.daily_usd,
                "x402_per_payment_usd": self.x402_per_payment_usd,
                "x402_max_window_sec": self.x402_max_window_sec,
                "chains": list(self.chains), "network": self.network,
                "operational_venue": self.operational_venue,
                "hyperliquid_orders": self.hyperliquid_orders,
                "sweep": bool(self.sweep_destination)}


def _cap(table: dict, key: str, *, required: bool = True,
         default: Optional[float] = None) -> float:
    if key not in table:
        if required:
            raise SignerConfigError(f"[caps] {key} is required (066 D6: the value in use now)")
        return float(default)
    raw = table[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SignerConfigError(f"[caps] {key} must be a number, got {raw!r}")
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise SignerConfigError(f"[caps] {key} must be finite and >= 0, got {raw!r}")
    return value


def _check_tables(data: dict) -> None:
    for name in _RETIRED_TABLES:
        if name in data:
            logger.warning("signer.toml: the [%s] table is retired (069 v4: no account binding) "
                           "and is ignored — remove it", name)
    unknown = set(data) - set(_TABLES) - set(_RETIRED_TABLES)
    if unknown:
        raise SignerConfigError(f"unknown table(s) {sorted(unknown)}")
    for name, keys in _TABLES.items():
        table = data.get(name, {})
        if not isinstance(table, dict):
            raise SignerConfigError(f"[{name}] is not a table")
        if keys is None:
            bad = [k for k in table if not _ENV_NAMES.match(str(k))]
            if bad:
                raise SignerConfigError(f"[env] may only pin RPC endpoints; refused {bad}")
            continue
        extra = set(table) - keys
        if extra:
            raise SignerConfigError(f"[{name}] unknown key(s) {sorted(extra)}")


def parse_signer_config(data: dict) -> SignerConfig:
    _check_tables(data)
    caps = data.get("caps", {})
    policy = data.get("policy", {})
    server = data.get("server", {})
    identity = data.get("identity", {})
    sweep = data.get("sweep", {})
    per_tx = _cap(caps, "per_tx_usd")
    daily = _cap(caps, "daily_usd")
    x402 = _cap(caps, "x402_per_payment_usd", required=False, default=per_tx)
    chains = policy.get("chains", [])
    if not isinstance(chains, list) or not all(isinstance(c, str) and c for c in chains):
        raise SignerConfigError("[policy] chains must be a list of chain names")
    uids = server.get("client_uids", [])
    if not isinstance(uids, list) or not all(isinstance(u, int) and not isinstance(u, bool)
                                            and u > 0 for u in uids):
        raise SignerConfigError("[server] client_uids must be a list of positive UIDs "
                                "(root is always the owner, never a client)")
    from core.signer.peer import DEFAULT_PEER_CHECK, PEER_CHECK_MODES
    peer_check = server.get("peer_check", DEFAULT_PEER_CHECK)
    if peer_check not in PEER_CHECK_MODES:
        raise SignerConfigError("[server] peer_check must be one of "
                                + ", ".join(PEER_CHECK_MODES))
    expected = {}
    for venue in ("treasury", "x402", "polymarket", "hyperliquid", "solana"):
        if venue in identity:
            addr = identity[venue]
            if not isinstance(addr, str) or not addr:
                raise SignerConfigError(f"[identity] {venue} must be an address string")
            if venue != "solana" and not _ADDRESS.match(addr):
                raise SignerConfigError(f"[identity] {venue} is not a 0x address")
            expected[venue] = addr
    dest = sweep.get("destination")
    if dest is not None and (not isinstance(dest, str) or not _ADDRESS.match(dest)):
        raise SignerConfigError("[sweep] destination must be a 0x address")
    tokens = sweep.get("tokens", {})
    if not isinstance(tokens, dict) or not all(
            isinstance(k, str) and ":" in k and isinstance(v, str) and _ADDRESS.match(v)
            for k, v in tokens.items()):
        raise SignerConfigError('[sweep] tokens must map "chain:SYMBOL" to a 0x address')

    def _int(table, key, default, name):
        v = table.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise SignerConfigError(f"[{name}] {key} must be a positive integer")
        return v

    hl = policy.get("hyperliquid_orders", False)
    if not isinstance(hl, bool):
        raise SignerConfigError("[policy] hyperliquid_orders must be true or false")
    venue = str(identity.get("operational_venue", "treasury")).strip().lower() or "treasury"
    if venue not in ("treasury", "x402"):
        raise SignerConfigError("[identity] operational_venue must be treasury or x402")
    return SignerConfig(
        per_tx_usd=per_tx, daily_usd=daily, x402_per_payment_usd=x402,
        chains=tuple(chains),
        socket=str(server.get("socket") or "/run/polyrob-signer/signer.sock"),
        socket_group=(str(server["socket_group"]) if server.get("socket_group") else None),
        state_dir=str(server.get("state_dir") or "/var/lib/polyrob-signer"),
        client_uids=tuple(uids),
        peer_check=peer_check,
        approval_ttl_sec=_int(policy, "approval_ttl_sec", 3600, "policy"),
        x402_max_window_sec=_int(policy, "x402_max_window_sec", 600, "policy"),
        hyperliquid_orders=hl,
        network=str(identity.get("network", "mainnet")),
        operational_venue=venue,
        expected_addresses=expected,
        sweep_destination=dest,
        sweep_tokens=dict(tokens),
        env={str(k): str(v) for k, v in data.get("env", {}).items()},
    )


def load_signer_config(path: str) -> SignerConfig:
    import tomllib
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise SignerConfigError(f"{path} does not exist") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SignerConfigError(f"{path} is unreadable: {exc}") from exc
    return parse_signer_config(data)


def _toml_str(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_signer_toml(cfg: SignerConfig) -> str:
    """The inverse of :func:`parse_signer_config` (used by provisioning)."""
    lines = ["# /etc/polyrob/signer.toml — written by deployment/hardening/install-signer.sh",
             "# (066 P2). The hard caps are the values IN USE when it ran (owner D6).",
             "# Root-owned; the agent cannot change it. Edit, then restart polyrob-signer.",
             "", "[caps]",
             f"per_tx_usd = {cfg.per_tx_usd!r}",
             f"daily_usd = {cfg.daily_usd!r}",
             f"x402_per_payment_usd = {cfg.x402_per_payment_usd!r}",
             "", "[policy]",
             "chains = [" + ", ".join(_toml_str(c) for c in cfg.chains) + "]",
             f"approval_ttl_sec = {cfg.approval_ttl_sec}",
             f"x402_max_window_sec = {cfg.x402_max_window_sec}",
             f"hyperliquid_orders = {'true' if cfg.hyperliquid_orders else 'false'}",
             "", "[server]",
             f"socket = {_toml_str(cfg.socket)}",
             f"state_dir = {_toml_str(cfg.state_dir)}",
             "client_uids = [" + ", ".join(str(u) for u in cfg.client_uids) + "]",
             f"peer_check = {_toml_str(cfg.peer_check)}"]
    if cfg.socket_group:
        lines.append(f"socket_group = {_toml_str(cfg.socket_group)}")
    lines += ["", "[identity]", f"network = {_toml_str(cfg.network)}",
              f"operational_venue = {_toml_str(cfg.operational_venue)}"]
    for venue, addr in sorted(cfg.expected_addresses.items()):
        lines.append(f"{venue} = {_toml_str(addr)}")
    if cfg.sweep_destination or cfg.sweep_tokens:
        lines += ["", "[sweep]"]
        if cfg.sweep_destination:
            lines.append(f"destination = {_toml_str(cfg.sweep_destination)}")
        if cfg.sweep_tokens:
            lines.append("tokens = { " + ", ".join(
                f"{_toml_str(k)} = {_toml_str(v)}" for k, v in sorted(cfg.sweep_tokens.items()))
                + " }")
    if cfg.env:
        lines += ["", "[env]"] + [f"{k} = {_toml_str(v)}" for k, v in sorted(cfg.env.items())]
    return "\n".join(lines) + "\n"


def apply_env(cfg: SignerConfig) -> None:
    """Put the ``[env]`` RPC pins into THIS process (the signer) before any read."""
    for k, v in cfg.env.items():
        os.environ[k] = v
