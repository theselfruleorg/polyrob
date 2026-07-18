"""Versioned key-derivation for the agent wallet (money-critical, fail-fast).

Schemes:
- "legacy": PBKDF2-HMAC-SHA256 over the raw master-seed string with the
  domain-separated per-venue label — the original scheme. Any install whose
  wallet predates derivation metadata is legacy FOREVER (addresses must never
  change while funds may exist).
- "bip44": standard BIP-44 HD derivation from a BIP-39 mnemonic
  (m/44'/60'/0'/0/{index}), so the mnemonic imports into MetaMask/Rabby and
  account 0 there == the treasury venue here.

The active scheme is recorded ONCE in <data-home>/wallet/meta.json (data-home =
POLYROB_DATA_DIR if set, else core.runtime_paths.resolve_data_home() — see
core.wallet.audit_sink._wallet_data_dir, the single resolution shared by init
and every later resolve, so init-time and runtime always agree) by
`polyrob wallet init` / import. Absence of the file = legacy. The scheme is
NEVER inferred from the seed's shape. AGENT_WALLET_DERIVATION env overrides
(recovery hatch only).
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

VENUE_INDEX = {"treasury": 0, "x402": 1, "polymarket": 2, "hyperliquid": 3}
SCHEMES = ("legacy", "bip44")
_PBKDF2_ITERS = 100_000


def wallet_meta_path(data_dir: Optional[Path] = None) -> Path:
    if data_dir is not None:
        return Path(data_dir) / "meta.json"
    # Same home the audit sink writes to (POLYROB_DATA_DIR / resolve_data_home()
    # anchored, not bare CWD — Finding 1, 2026-07-14) — one resolution shared by
    # both so init-time and runtime always agree. for_meta=True fails CLOSED if the
    # data-home can't be resolved rather than risking a CWD-relative meta path (L3).
    from core.wallet.audit_sink import _wallet_data_dir
    return Path(_wallet_data_dir(for_meta=True)) / "meta.json"


def is_valid_mnemonic(text: str) -> bool:
    try:
        from eth_account import Account
        Account.enable_unaudited_hdwallet_features()
        Account.from_mnemonic((text or "").strip(), account_path="m/44'/60'/0'/0/0")
        return True
    except Exception:
        return False


def derive_key(seed: str, venue: str, scheme: str) -> bytes:
    if venue not in VENUE_INDEX:
        raise ValueError(f"unknown venue '{venue}' (expected one of {sorted(VENUE_INDEX)})")
    if scheme == "legacy":
        label = f"agent-wallet:{venue}".encode("utf-8")
        return hashlib.pbkdf2_hmac("sha256", seed.encode("utf-8"), label,
                                   _PBKDF2_ITERS, dklen=32)
    if scheme == "bip44":
        try:
            from eth_account import Account
        except ImportError as e:  # crypto extra absent
            raise ValueError(
                "bip44 derivation needs eth-account — pip install 'polyrob[crypto]'") from e
        Account.enable_unaudited_hdwallet_features()
        path = f"m/44'/60'/0'/0/{VENUE_INDEX[venue]}"
        try:
            acct = Account.from_mnemonic(seed.strip(), account_path=path)
        except Exception as e:
            raise ValueError(
                "AGENT_WALLET_MASTER_SEED is not a valid BIP-39 mnemonic but "
                "derivation is 'bip44' — fix the seed or set AGENT_WALLET_DERIVATION=legacy"
            ) from e
        return bytes(acct.key)
    raise ValueError(f"unknown derivation scheme '{scheme}' (expected one of {SCHEMES})")


def resolve_scheme(env: Optional[Mapping[str, str]] = None,
                   data_dir: Optional[Path] = None) -> str:
    import os
    env = os.environ if env is None else env
    override = (env.get("AGENT_WALLET_DERIVATION") or "").strip().lower()
    if override:
        # A recovery-hatch override must be a real scheme. Silently ignoring a typo
        # (e.g. 'bip-44') let an operator believe they'd recovered while nothing
        # changed. H2, 2026-07-15.
        if override in SCHEMES:
            return override
        raise ValueError(
            f"AGENT_WALLET_DERIVATION={override!r} is not a valid scheme "
            f"(expected one of {SCHEMES}); unset it or use a valid value")
    # Three-state, money-critical: file ABSENT is the ONLY 'legacy' signal (a genuine
    # pre-meta install has no meta.json at all). A present-but-unreadable/corrupt file
    # or an unknown recorded scheme MUST fail fast — silently returning 'legacy' for a
    # funded bip44 wallet would flip its addresses (PBKDF2 digests a mnemonic string
    # without error). H2, 2026-07-15.
    meta = wallet_meta_path(data_dir)
    try:
        present = meta.is_file()
    except Exception:
        present = False
    if not present:
        return "legacy"
    try:
        recorded = (json.loads(meta.read_text()).get("derivation") or "").strip().lower()
    except Exception as e:
        raise ValueError(
            f"wallet derivation meta {meta} exists but is unreadable ({e}); restore it "
            f"or set AGENT_WALLET_DERIVATION explicitly — refusing to fall back to "
            f"'legacy' (a bip44 wallet's addresses would change)") from e
    if recorded in SCHEMES:
        return recorded
    raise ValueError(
        f"wallet derivation meta {meta} records an unknown scheme {recorded!r}; "
        f"restore it or set AGENT_WALLET_DERIVATION explicitly")


def maybe_warn_legacy_mnemonic(seed: str, scheme: str,
                               data_dir: Optional[Path] = None) -> Optional[str]:
    """H1 (core-side minimum warning, 2026-07-15): if the resolved scheme is
    'legacy' but the seed is a valid BIP-39 mnemonic AND no meta.json exists, the
    wallet is deriving PBKDF2 addresses from a mnemonic STRING — almost always the
    scheme-flip footgun (init recorded bip44 under a different data-home; a run from
    another directory missed the meta). That silently changes the funded treasury/
    x402 address and makes `wallet export` print keys that don't control the funds,
    so warn loudly. Returns the message (also logged at WARNING) or None. Fail-open:
    a genuine legacy install (raw seed, not a mnemonic) or a deliberately recorded
    scheme (meta present) never trips this.
    """
    if scheme != "legacy" or not seed or not is_valid_mnemonic(seed):
        return None
    try:
        meta_present = wallet_meta_path(data_dir).is_file()
    except Exception:
        meta_present = False
    if meta_present:
        return None
    msg = (
        "AGENT_WALLET_MASTER_SEED looks like a valid BIP-39 mnemonic but the wallet "
        "is deriving addresses with the LEGACY (PBKDF2) scheme and no derivation "
        "meta.json exists — the treasury/x402 addresses will NOT match MetaMask "
        "account 0 and may differ from a bip44 wallet you already funded. If you "
        "meant bip44, set AGENT_WALLET_DERIVATION=bip44 (or re-run `polyrob wallet "
        "init` from the correct data-home) BEFORE funding. See review H1."
    )
    logger.warning(msg)
    return msg


def write_scheme_once(scheme: str, data_dir: Optional[Path] = None) -> Path:
    if scheme not in SCHEMES:
        raise ValueError(f"unknown derivation scheme '{scheme}'")
    meta = wallet_meta_path(data_dir)
    if meta.is_file():
        existing = (json.loads(meta.read_text()).get("derivation") or "").strip().lower()
        if existing == scheme:
            return meta
        raise ValueError(
            f"wallet derivation already recorded as '{existing}' — refusing to switch to "
            f"'{scheme}' (addresses would change; funds could strand)")
    meta.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    meta.write_text(json.dumps({
        "derivation": scheme,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }, indent=2))
    return meta
