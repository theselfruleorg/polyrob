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



class AgentUriTooLarge(ValueError):
    """The encoded registration file exceeds what may go on-chain."""


def instance_agent_uri() -> str:
    """The instance's own registration document, checked before encoding."""
    from modules.eip8004.registration import build_registration_file
    from core.secret_scrub import scrub_secret_shapes
    base_url = os.environ.get("A2A_BASE_URL")
    document = build_registration_file(base_url or "http://localhost:9000").model_dump(exclude_none=True)
    text = json.dumps(document, ensure_ascii=False)
    if scrub_secret_shapes(text) != text:
        raise ValueError("Registration document contains credential material")
    return build_agent_uri(document, base_url=base_url)


def _is_public(base_url: Optional[str]) -> bool:
    """The ONE public-host rule (``modules.eip8004.registration``)."""
    from modules.eip8004.registration import is_public_base_url
    return is_public_base_url(base_url)


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
    """A refusal when this wallet already holds its self-minted identity.

    ⚠️ Read from the CHAIN, never from a local flag: a fresh data dir would lose
    the flag and re-register, minting a second token. The chain is the only
    thing that actually knows.
    """
    if existing_agent_id is None:
        # agentId 0 is a REAL id (the registries mint from 0; 4663's first identity is 0) —
        # `not 0` read it as "unregistered" and would mint a second identity (testnet-run F4).
        return None
    return (
        f"this wallet already holds its self-minted ERC-8004 identity on {chain} "
        f"(agentId {existing_agent_id}). `register()` is not idempotent — "
        f"calling it again mints a SECOND token and splits the identity. "
        f"Change the registration file with `set_agent_uri` instead.")


#: How long a broadcast registration stays "in flight" for the double-mint
#: check. A receipt wait times out at 120 s, so a registration still unseen by
#: the chain after this long was dropped or replaced.
IN_FLIGHT_WINDOW_SEC = 24 * 3600


def in_flight_registration(audit, *, chain: str, now: float,
                           window: float = IN_FLIGHT_WINDOW_SEC) -> Optional[str]:
    """A refusal when a registration on *chain* was BROADCAST recently, else None.

    CR-L15: the chain read in :func:`read_agent_id` cannot see a registration
    that is signed but not yet mined — a pending receipt (or a second call
    racing the first) reads "not registered" and mints a second identity. The
    ledger records every broadcast registration, so it is the in-flight signal.
    An unreadable ledger refuses: fail closed.
    """
    try:
        rows = list(audit or ())
    except Exception:
        return ("could not read the spend ledger to rule out a registration "
                "already in flight; refusing rather than risking a SECOND identity")
    for row in reversed(rows):
        try:
            if (row.get("action") == "register_agent"
                    and str(row.get("chain") or "") == chain
                    and now - float(row.get("ts") or 0.0) <= window):
                return (f"a registration on {chain} was already broadcast "
                        f"(tx {row.get('result_ref')}) and the chain does not "
                        f"show its agent token yet. Check that transaction "
                        f"before registering again — a second call mints a "
                        f"SECOND identity.")
        except Exception:
            return ("the spend ledger holds an unreadable row; refusing rather "
                    "than risking a SECOND identity")
    return None


def minted_agent_id_from_receipt(raw_receipt: Any, *, registry: str,
                                 holder: str) -> Optional[int]:
    """The agentId the LANDED receipt minted to *holder*, or None.

    CR-M13: the simulated id is a prediction — a concurrent registration by
    anyone else takes that id first. Only the receipt's
    ``Transfer(0x0 -> holder, tokenId)`` from the pinned registry says which
    token is ours. None when the logs are unreadable, show no such mint, or
    show more than one.
    """
    from core.wallet.simulation import _TOPIC_TRANSFER
    if not isinstance(raw_receipt, dict) or not isinstance(raw_receipt.get("logs"), list):
        return None
    zero = "0x" + "0" * 64
    to_word = "0x" + str(holder)[2:].lower().rjust(64, "0")
    minted = []
    for log in raw_receipt["logs"]:
        try:
            topics = [str(t).lower() for t in (log.get("topics") or ())]
            if (str(log.get("address") or "").lower() != str(registry).lower()
                    or len(topics) != 4 or topics[0] != _TOPIC_TRANSFER):
                continue
            if topics[1] == zero and topics[2] == to_word:
                minted.append(int(topics[3], 16))
        except (AttributeError, TypeError, ValueError):
            return None
    return minted[0] if len(minted) == 1 else None


#: ``supportsInterface`` id of ERC721Enumerable (``tokenOfOwnerByIndex``).
_ENUMERABLE_IID = "780e9d63"
#: Blocks per ``eth_getLogs`` window. The public 4663 node refuses a span above 10,000,000
#: blocks (measured 2026-09-29); half that leaves headroom.
LOG_SCAN_STEP = 5_000_000


def _supports_enumeration(rpc, registry: str) -> bool:
    """True only when the registry SAYS it is ERC721Enumerable. A failed or malformed read
    is False: the log path below proves the id on its own, so doubt costs nothing."""
    try:
        raw = rpc("eth_call", [{"to": registry, "data": "0x01ffc9a7" + _ENUMERABLE_IID.ljust(64, "0")},
                               "latest"])
        return isinstance(raw, str) and raw.startswith("0x") and len(raw) >= 66 and int(raw[2:66], 16) == 1
    except Exception:  # noqa: BLE001
        return False


def read_agent_id(rpc, *, chain: str, holder: str, from_block: Optional[int] = None,
                  step: int = LOG_SCAN_STEP) -> Optional[int]:
    """This wallet's existing agentId on *chain*, or None.

    ⚠️ Returns None ONLY when the read succeeded and found nothing. A failed
    read RAISES, because "I could not look" must never be mistaken for "not
    registered" — that mistake mints a second identity.

    The registry's Transfer logs must account for every held token. Only an
    identity MINTED to this holder is its registration; unsolicited transfers
    are holdings, never authority and never a reason to block registration.
    Multiple self-minted live identities are ambiguous and refuse.
    """
    registry = erc8004.resolve_identity_registry(chain)
    from core.wallet.abi import decode
    data = encode_call("balanceOf", [{"type": "address"}], [holder])
    raw = rpc("eth_call", [{"to": registry, "data": data}, "latest"])
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise RuntimeError(
            f"could not read the ERC-8004 registry on {chain}; refusing to "
            f"assume this wallet is unregistered")
    held = int(decode([{"type": "uint256"}], raw)[0])
    if held == 0:
        return None
    return _agent_id_from_logs(rpc, chain=chain, registry=registry, holder=holder, held=held,
                               from_block=from_block, step=step)


def _agent_id_from_logs(rpc, *, chain: str, registry: str, holder: str, held: int,
                        from_block: Optional[int], step: int) -> Optional[int]:
    from core.wallet.abi import decode
    from core.wallet.simulation import _TOPIC_TRANSFER
    start = erc8004.identity_logs_from(chain) if from_block is None else int(from_block)
    head_raw = rpc("eth_blockNumber", [])
    head = int(head_raw, 16) if isinstance(head_raw, str) else int(head_raw)
    to_word = "0x" + str(holder).lower().removeprefix("0x").rjust(64, "0")
    candidates = set()
    minted = set()
    while start <= head:
        end = min(start + int(step) - 1, head)
        rows = rpc("eth_getLogs", [{"address": registry, "topics": [_TOPIC_TRANSFER, None, to_word],
                                    "fromBlock": hex(start), "toBlock": hex(end)}])
        if not isinstance(rows, list):
            raise RuntimeError(f"the ERC-8004 registry's Transfer logs on {chain} were unreadable "
                               f"(blocks {start}..{end}); refusing to assume an agentId")
        for row in rows:
            topics = [str(t).lower() for t in (row.get("topics") or ())]
            if (len(topics) == 4 and topics[0] == _TOPIC_TRANSFER and topics[2] == to_word
                    and str(row.get("address") or "").lower() == registry.lower()):
                candidate = int(topics[3], 16)
                candidates.add(candidate)
                if topics[1] == "0x" + "0" * 64:
                    minted.add(candidate)
        start = end + 1
    live = []
    for agent_id in sorted(candidates):
        raw = rpc("eth_call", [{"to": registry, "data": encode_call("ownerOf", [_U256], [agent_id])},
                               "latest"])
        if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 66:
            raise RuntimeError(f"could not read ownerOf({agent_id}) on the ERC-8004 registry "
                               f"({chain}); refusing to assume an agentId")
        if str(decode([{"type": "address"}], raw)[0]).lower() == str(holder).lower():
            live.append(agent_id)
    if len(live) != held:
        raise RuntimeError(
            f"this wallet holds {held} ERC-8004 agent token(s) on {chain} but the registry's "
            f"Transfer logs from block {erc8004.identity_logs_from(chain) if from_block is None else from_block} "
            f"name {live or 'none'} — refusing to guess the agentId")
    own = sorted(minted.intersection(live))
    if len(own) > 1:
        raise RuntimeError("multiple self-minted ERC-8004 identities; owner must resolve the ambiguity")
    return own[0] if own else None
