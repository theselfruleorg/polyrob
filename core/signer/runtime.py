"""Start-up of the ``polyrob-signer`` process (066 §5.1, §5.5).

Order matters and is pinned by a test:

1. read ``signer.toml`` (refuse to start on a bad one);
2. point the wallet home at the signer's OWN state dir (``POLYROB_DATA_DIR``)
   and apply the ``[env]`` RPC pins;
3. make the process non-dumpable (066 P0.1) — on Linux a failure refuses to start;
4. take the key material out of ``os.environ`` (066 P0.2);
5. derive the wallet and check its addresses against ``[identity]`` — a signer
   that would sign from a DIFFERENT wallet than the one of record refuses to
   start (wallet continuity, 066 §5.5).
"""
import logging
import os
from typing import Callable, Dict, List, Mapping, Optional

from core.signer.caps import SignerConfig, apply_env, load_signer_config

logger = logging.getLogger(__name__)


class SignerStartupError(RuntimeError):
    """The signer refuses to start (bad policy, dumpable, no seed, wrong wallet)."""


def build_signer_wallet(cfg: SignerConfig, seed: str):
    """The signer's AgentWallet: the hard caps are its gate; its ledger is the
    signer's own ``<state_dir>/wallet/audit.jsonl``."""
    from core.wallet.agent_wallet import AgentWallet
    from core.wallet.audit_sink import JsonlAuditSink
    from core.wallet.config import WalletConfig, normalize_master_seed
    wcfg = WalletConfig(
        enabled=True, backend="local_eoa",
        master_seed=normalize_master_seed(seed),
        network=cfg.network if cfg.network in ("testnet", "mainnet") else "mainnet",
        max_per_tx_usd=cfg.per_tx_usd, x402_client_enabled=False,
        x402_facilitator_url="", daily_cap_usd=cfg.daily_usd,
        operational_venue=cfg.operational_venue)
    sink = JsonlAuditSink(os.path.join(cfg.state_dir, "wallet", "audit.jsonl"))
    return AgentWallet(wcfg, audit_sink=sink)


def continuity_problems(identity: Mapping, expected: Mapping[str, str]) -> List[str]:
    """Each address of record that the derived identity does NOT reproduce."""
    out = []
    evm = dict(identity.get("evm") or {})
    for venue, want in sorted(expected.items()):
        have = identity.get("solana") if venue == "solana" else evm.get(venue)
        if venue == "solana":
            same = have == want
        else:
            same = isinstance(have, str) and have.lower() == str(want).lower()
        if not same:
            out.append(f"{venue}: derived {have or '(none)'} but the record says {want}")
    return out


def prepare_process(cfg: SignerConfig, *, harden: Callable = None) -> None:
    os.environ["POLYROB_DATA_DIR"] = cfg.state_dir
    apply_env(cfg)
    from core.security import process_hardening as ph
    result = (harden or ph.harden_custody_process)()
    if ph._is_linux() and not result.held:
        raise SignerStartupError(f"the signer must be non-dumpable: {result.detail}")
    from core.security.custody_env import take_custody_secrets
    take_custody_secrets()


def build_service(cfg: SignerConfig, *, price_fn=None, fallback_price_fn=None,
                  secret_fn: Optional[Callable] = None, **service_kw):
    from core.security.custody_env import custody_secret
    from core.signer.server import SignerService
    from core.signer.store import SignerStore
    secret_fn = secret_fn or custody_secret
    seed = secret_fn("AGENT_WALLET_MASTER_SEED")
    if not seed or len(seed) < 32:
        raise SignerStartupError("AGENT_WALLET_MASTER_SEED is not in the signer's env "
                                 "(EnvironmentFile=/etc/polyrob/wallet.env)")
    wallet = build_signer_wallet(cfg, seed)
    service = SignerService(cfg, wallet, store=SignerStore(cfg.state_dir), price_fn=price_fn,
                            fallback_price_fn=fallback_price_fn, secret_fn=secret_fn, **service_kw)
    problems = continuity_problems(service.identity(), cfg.expected_addresses)
    if problems:
        raise SignerStartupError("wallet continuity REFUSED — the signer's seed is not the "
                                 "wallet of record: " + "; ".join(problems))
    if not cfg.expected_addresses:
        logger.warning("signer.toml [identity] records no address: continuity is unchecked")
    return service


def start(config_path: str, *, price_fn=None, fallback_price_fn=None):
    """``(service, server)``, ready to ``serve_forever``."""
    from core.signer.server import SignerServer
    cfg = load_signer_config(config_path)
    prepare_process(cfg)
    service = build_service(cfg, price_fn=price_fn, fallback_price_fn=fallback_price_fn)
    return service, SignerServer(service, cfg.socket, group=cfg.socket_group)


def identity_summary(service) -> Dict[str, object]:
    ident = service.identity()
    return {"treasury": ident["evm"].get("treasury"), "solana": ident.get("solana"),
            "scheme": ident.get("scheme")}
