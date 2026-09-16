"""ERC-8004 self-registration — the agent mints its own identity (046 step 4).

The Identity Registry **is** an ERC-721 collection: `register()` mints a token
to the caller and the returned `agentId` **is** the tokenId. So registering does
not need a mint service, a voucher or a marketplace — the agent's own wallet
calls a pinned contract and owns the result.

⚠️ THE AGENT URI NEEDS NO HOSTING. The spec says agents SHOULD use a
base64-encoded `data:` URI, so the whole registration file rides inside the
transaction. A self-hoster with no domain, no IPFS account and no public
endpoint can register. An instance that HAS a public base URL uses it instead
and gets a file it can update without a transaction. Neither is a one-way door:
`setAgentURI` moves between them later.

⚠️ THE DESTINATION IS PINNED, NEVER SUPPLIED. Everything here resolves through
`core.wallet.erc8004`, which refuses a non-matching env override. A
caller-supplied registry would make this "call an arbitrary contract from the
treasury wallet" wearing a registration's name.

⚠️ `register()` IS NOT IDEMPOTENT. A second call mints a SECOND token and splits
the identity — two agentIds, neither of which is authoritative.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, Optional

from core.wallet import erc8004
from core.wallet.abi import encode_call
from core.wallet.tx_guard import TxIntent

logger = logging.getLogger(__name__)

_STR = {"type": "string"}
_U256 = {"type": "uint256"}

#: Ceiling on the encoded agentURI. Calldata is priced per byte and a
#: registration is a once-ever act, so this is generous — but it is a REFUSAL,
#: never a truncation: a half-written identity on-chain is permanent.
MAX_AGENT_URI_BYTES = 16_384

#: Hosts nobody outside this machine can resolve.
_PRIVATE_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


class AgentUriTooLarge(ValueError):
    """The encoded registration file exceeds what may go on-chain."""


def _is_public(base_url: Optional[str]) -> bool:
    if not base_url:
        return False
    low = str(base_url).lower()
    if not low.startswith(("http://", "https://")):
        return False
    host = low.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    return host not in _PRIVATE_HOSTS and "." in host


def uri_mode() -> str:
    """``auto`` (hosted when public, else data) | ``data`` | ``hosted``."""
    mode = (os.getenv("EIP8004_AGENT_URI_MODE") or "auto").strip().lower()
    return mode if mode in ("auto", "data", "hosted") else "auto"


def build_agent_uri(document: Dict[str, Any],
                    base_url: Optional[str]) -> str:
    """The `agentURI` for *document*.

    Hosted when there is a genuinely public base URL (and the mode allows it),
    otherwise a base64 `data:` URI carrying the whole file.
    """
    mode = uri_mode()
    public = _is_public(base_url)
    if mode == "hosted":
        if not public:
            raise ValueError(
                "EIP8004_AGENT_URI_MODE=hosted, but there is no public base URL "
                "to serve the registration file from. Set A2A_BASE_URL to a "
                "reachable https:// host, or use the default `auto`/`data` mode, "
                "which needs no hosting at all.")
        return f"{str(base_url).rstrip('/')}/eip8004/registration.json"
    if mode == "auto" and public:
        return f"{str(base_url).rstrip('/')}/eip8004/registration.json"

    # ⚠️ `separators` + `sort_keys` so the encoding is deterministic: the file
    # IS the identity once it is on-chain, and a re-encode that differs by
    # whitespace would read as a different document.
    raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    uri = "data:application/json;base64," + base64.b64encode(raw).decode("ascii")
    if len(uri.encode("utf-8")) > MAX_AGENT_URI_BYTES:
        raise AgentUriTooLarge(
            f"the registration file encodes to {len(uri)} bytes, over the "
            f"{MAX_AGENT_URI_BYTES}-byte on-chain ceiling. Shorten the "
            f"description, or host the file on a public URL and set "
            f"A2A_BASE_URL. It is NOT truncated — a half-written identity "
            f"on-chain is permanent.")
    return uri


def encode_register(agent_uri: str) -> str:
    """`register(string agentURI)` calldata — the one-argument overload.

    The metadata overload is deliberately unused: on-chain key/value metadata is
    a second place for the identity to live and disagree with the file.
    """
    return encode_call("register", [_STR], [str(agent_uri)])


def encode_set_agent_uri(agent_id: int, new_uri: str) -> str:
    """`setAgentURI(uint256, string)` — update the file after registration."""
    return encode_call("setAgentURI", [_U256, _STR], [int(agent_id), str(new_uri)])


def build_registration_intent(*, chain: str, max_spend_usd: float,
                              idempotency_key: Optional[str]) -> TxIntent:
    """Declare the registration so the SIMULATION adjudicates it.

    `amount_raw=0` and `token=None`: nothing leaves. What must ARRIVE is the
    agent token, and `expected_registry` is what rule 6f holds the transaction
    to — the difference between "I registered" and "I called a contract that
    took my gas".
    """
    registry = erc8004.resolve_identity_registry(chain)  # raises on an unknown chain
    return TxIntent(
        chain=chain, token=None, to=registry, amount_raw=0,
        max_spend_usd=float(max_spend_usd), idempotency_key=idempotency_key,
        is_registration=True, expected_registry=registry,
    )


def check_not_already_registered(*, existing_agent_id: Optional[int],
                                 chain: str) -> Optional[str]:
    """A refusal string when this wallet already holds an identity, else None.

    ⚠️ Read from the CHAIN, never from a local flag: a fresh data dir would lose
    the flag and re-register, minting a second token. The chain is the only
    thing that actually knows.
    """
    if not existing_agent_id:
        return None
    return (
        f"already registered on {chain} as agentId {existing_agent_id}. "
        f"`register()` is not idempotent — calling it again mints a SECOND "
        f"token and splits the identity, leaving two agentIds and no way to say "
        f"which is authoritative. To change the registration file use "
        f"`set_agent_uri` instead.")


def read_agent_id(rpc, *, chain: str, holder: str) -> Optional[int]:
    """This wallet's existing agentId on *chain*, or None.

    ⚠️ Returns None ONLY when the read succeeded and found nothing. A failed
    read RAISES, because "I could not look" must never be mistaken for "not
    registered" — that mistake mints a second identity.
    """
    registry = erc8004.resolve_identity_registry(chain)
    from core.wallet.abi import decode
    data = encode_call("balanceOf", [{"type": "address"}], [holder])
    raw = rpc("eth_call", [{"to": registry, "data": data}, "latest"])
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise RuntimeError(
            f"could not read the ERC-8004 registry on {chain}; refusing to "
            f"assume this wallet is unregistered")
    if decode([{"type": "uint256"}], raw)[0] == 0:
        return None
    data = encode_call("tokenOfOwnerByIndex",
                       [{"type": "address"}, _U256], [holder, 0])
    raw = rpc("eth_call", [{"to": registry, "data": data}, "latest"])
    if not isinstance(raw, str) or not raw.startswith("0x") or raw == "0x":
        # It holds a token but enumeration is unavailable. Still registered —
        # say so without inventing an id.
        return -1
    return int(decode([_U256], raw)[0])
