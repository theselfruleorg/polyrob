"""Non-fungible verbs — see, send, revoke. Never blanket-grant, never buy.

The agent could not hold a collectible safely, let alone use one. Two halves were
missing and both are closed now: the guard was structurally blind to every
ERC-721/ERC-1155 event (`core/wallet/simulation.py`, 2026-09-15), and no verb
could read or move one. This is the second half.

The verbs extend the EXISTING ``defi_trade`` / ``defi_data`` tools rather than
adding a ``tools/nft/`` id. A new tool_id costs its own capability row,
descriptor, two pin tests, a parity row and a separate operator grant — while
``defi_trade`` is already ``{money, high_impact, delegate_blocked}`` and already
the EVM write surface. Reusing it inherits every gate by construction.

⚠️ THERE IS NO GRANT VERB, and that absence is load-bearing.
``setApprovalForAll(operator, true)`` is a standing claim on every token of a
collection, present and future, and it is the single most common way a
self-custodial wallet is drained. ``tx_guard`` refuses ANY observed grant, and
:func:`encode_revoke_operator` takes no ``approved`` argument — a boolean
parameter is an invitation, so the value is not expressible.

⚠️ AN NFT IS UNPRICEABLE. Its transaction cost is the worst-case FEE (priced in
``tx_guard``), and what bounds the MOVE is the owner, not a cap:
``defi_trade_nft_transfer`` lives in
``core.config_policy.spend_lane.ALWAYS_OWNER_APPROVED_VERBS``, which
``DEFI_TIERED_SPEND_LANE`` can never exempt. Inventing a floor price would make
the cap lie about the asset class that is easiest to wash-trade.

⚠️ Enumeration ("list everything this address owns") is impossible from a plain
JSON-RPC node — there is no such method — so it needs an indexer. Without one we
say so. An empty list would read as "you own nothing", and "I could not look" is
a different fact.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from core.wallet.abi import encode_call, decode
from core.wallet.tx_guard import TxIntent

logger = logging.getLogger(__name__)

ERC721 = "erc721"
ERC1155 = "erc1155"
STANDARDS = (ERC721, ERC1155)

_ADDR = {"type": "address"}
_U256 = {"type": "uint256"}
_BYTES = {"type": "bytes"}


def _norm_addr(value: Any, what: str) -> str:
    s = str(value or "").strip()
    if not (s.startswith("0x") and len(s) == 42):
        raise ValueError(f"{what} must be a 0x-prefixed 20-byte address, got {value!r}")
    return s.lower()


def _norm_standard(standard: Any) -> str:
    s = str(standard or "").strip().lower().replace("-", "")
    if s not in STANDARDS:
        raise ValueError(
            f"unknown token standard {standard!r} — this rail handles "
            f"{' and '.join(STANDARDS)} only, and guessing which one a contract "
            f"implements would encode a call it may silently misread")
    return s


# --- calldata --------------------------------------------------------------

def encode_nft_transfer(*, standard: str, frm: str, to: str, token_id: int,
                        amount: int = 1) -> str:
    """``safeTransferFrom`` calldata for either standard.

    ``safeTransferFrom`` rather than ``transferFrom`` on purpose: it calls the
    receiver's ``onERC721Received``/``onERC1155Received`` hook, so a contract that
    cannot handle the token REVERTS instead of swallowing it forever.
    """
    std = _norm_standard(standard)
    frm, to = _norm_addr(frm, "from"), _norm_addr(to, "to")
    token_id = int(token_id)
    amount = int(amount)
    if token_id < 0:
        raise ValueError("token_id cannot be negative")
    if amount < 1:
        raise ValueError("amount must be at least 1")
    if std == ERC721:
        if amount != 1:
            # An ERC-721 token is unique. Coercing 5 to 1 would send something
            # other than what the caller asked for, quietly.
            raise ValueError(
                f"an ERC-721 token is unique, so amount must be 1 (got {amount}) "
                f"— did you mean erc1155?")
        return encode_call("safeTransferFrom", [_ADDR, _ADDR, _U256],
                           [frm, to, token_id])
    return encode_call("safeTransferFrom", [_ADDR, _ADDR, _U256, _U256, _BYTES],
                       [frm, to, token_id, amount, b""])


def encode_revoke_operator(*, operator: str) -> str:
    """``setApprovalForAll(operator, false)`` — a REVOKE, and only a revoke.

    ⚠️ There is deliberately no ``approved`` parameter. The grant direction is
    not expressible from anywhere in this tree; ``tx_guard`` refuses it besides.
    """
    return encode_call("setApprovalForAll", [_ADDR, {"type": "bool"}],
                       [_norm_addr(operator, "operator"), False])


# --- the intent handed to the ONE authorizer -------------------------------

def build_transfer_intent(*, chain: str, contract: str, standard: str,
                          token_id: int, amount: int, max_spend_usd: float,
                          idempotency_key: Optional[str]) -> TxIntent:
    """Declare the move so the SIMULATION, not the caller, adjudicates it.

    ``amount_raw=0`` and ``token=None``: nothing fungible leaves. What leaves is
    an identifier, which is what ``nft_out`` describes and what rules 6c/6d in
    ``tx_guard`` hold the transaction to.
    """
    std = _norm_standard(standard)
    c = _norm_addr(contract, "contract")
    return TxIntent(
        chain=chain, token=None, to=c, amount_raw=0,
        max_spend_usd=float(max_spend_usd), idempotency_key=idempotency_key,
        is_nft_op=True,
        nft_out=((c, std, int(token_id), int(amount)),),
    )


def build_revoke_intent(*, chain: str, contract: str, operator: str,
                        max_spend_usd: float,
                        idempotency_key: Optional[str]) -> TxIntent:
    """A revoke moves nothing and grants nothing; it retires a claim."""
    c = _norm_addr(contract, "contract")
    return TxIntent(
        chain=chain, token=None, to=c, amount_raw=0,
        max_spend_usd=float(max_spend_usd), idempotency_key=idempotency_key,
        is_nft_op=True,
        nft_operator_ops=((c, _norm_addr(operator, "operator"), False),),
    )


# --- reads (keyless where the chain can answer) ----------------------------

def _call(rpc, chain: str, to: str, data: str) -> Optional[str]:
    """One ``eth_call``; None when the node cannot answer (never a fake zero)."""
    try:
        out = rpc("eth_call", [{"to": to, "data": data}, "latest"])
        return out if isinstance(out, str) and out.startswith("0x") else None
    except Exception:
        logger.debug("nft read failed on %s %s", chain, to, exc_info=True)
        return None


#: The longest ``tokenURI`` kept from a read. A ``data:`` URI carries the whole metadata
#: (the avatar decodes up to 1 MiB of it, ~1.4 M base64 chars); a longer one is NOT
#: CHECKED, never cut (a cut ``data:`` URI does not decode).
MAX_URI_CHARS = 1_500_000
#: How much of a ``tokenURI`` the model sees (:func:`uri_for_model`).
URI_SHOWN_CHARS = 512


def uri_for_model(uri: Any, *, source: str = "nft tokenURI") -> str:
    """A ``tokenURI`` for the model: the COLLECTION wrote it (any contract can send the
    treasury a token, and a group member can ask for the read), so it is capped to
    :data:`URI_SHOWN_CHARS`, put on one line and fenced as untrusted data
    (``core.security.untrusted_wrap.wrap_untrusted``, the one fence). Audit 2026-11-21 L-15."""
    from core.security.untrusted_wrap import wrap_untrusted
    text = " ".join(str(uri or "").split())
    shown = text[:URI_SHOWN_CHARS] + ("…" if len(text) > URI_SHOWN_CHARS else "")
    return wrap_untrusted(source, shown)


def read_token_facts(rpc, *, chain: str, contract: str, token_id: int,
                     holder: Optional[str] = None) -> Dict[str, Any]:
    """Per-token facts straight from the chain. No key, no indexer, exact.

    Each field is ``None`` when the node did not answer, never a default — an
    unanswered ``ownerOf`` must not read as "nobody owns it".
    """
    c = _norm_addr(contract, "contract")
    facts: Dict[str, Any] = {"chain": chain, "contract": c,
                             "token_id": int(token_id), "standard": None,
                             "owner": None, "balance": None, "uri": None,
                             "not_checked": []}

    owner_raw = _call(rpc, chain, c, encode_call("ownerOf", [_U256], [int(token_id)]))
    if owner_raw and len(owner_raw) >= 66:
        try:
            facts["owner"] = "0x" + owner_raw[-40:]
            facts["standard"] = ERC721
        except Exception:
            facts["not_checked"].append("owner")
    else:
        facts["not_checked"].append("owner (ownerOf did not answer — an ERC-1155 "
                                    "has no single owner)")

    if holder:
        bal_raw = _call(rpc, chain, c,
                        encode_call("balanceOf", [_ADDR, _U256],
                                    [_norm_addr(holder, "holder"), int(token_id)]))
        if bal_raw:
            try:
                facts["balance"] = decode([_U256], bal_raw)[0]
                facts["standard"] = facts["standard"] or ERC1155
            except Exception:
                facts["not_checked"].append("balance")

    for sel, inputs in (("tokenURI", [_U256]), ("uri", [_U256])):
        raw = _call(rpc, chain, c, encode_call(sel, inputs, [int(token_id)]))
        if raw:
            try:
                uri = decode([{"type": "string"}], raw)[0]
                if len(uri) > MAX_URI_CHARS:
                    facts["not_checked"].append(
                        f"metadata uri (longer than {MAX_URI_CHARS:,} characters)")
                    break
                facts["uri"] = uri
                break
            except Exception:
                continue
    if facts["uri"] is None and not any(n.startswith("metadata uri")
                                        for n in facts["not_checked"]):
        facts["not_checked"].append("metadata uri")
    return facts


class EnumerationUnavailable(RuntimeError):
    """No indexer is configured, so holdings cannot be listed.

    ⚠️ Raised rather than returning ``[]``. "You own nothing" and "I could not
    look" are different facts and only one of them is reassuring — the same rule
    the `creations` status section follows.
    """


def enumerate_holdings(*, chain: str, address: str,
                       api_key: Optional[str] = None,
                       fetch: Optional[Any] = None,
                       contracts: tuple = ()) -> List[Dict[str, Any]]:
    """Every NFT ``address`` holds on ``chain``.

    ⚠️ A plain JSON-RPC node CANNOT answer this — there is no such method — so it
    needs an indexer. ``ALCHEMY_API_KEY`` is the one already in the tree
    (`tools/alchemy/`). Without it this raises
    :class:`EnumerationUnavailable` with the remedy.
    """
    import os
    key = api_key if api_key is not None else (os.getenv("ALCHEMY_API_KEY") or "").strip()
    if not key:
        raise EnumerationUnavailable(
            "no NFT enumeration provider is configured. Listing everything an "
            "address holds needs an indexer — a plain RPC node has no method for "
            "it. Set ALCHEMY_API_KEY to enable this read. (Per-token facts via "
            "nft_info still work with no key at all.)")
    if fetch is None:
        raise EnumerationUnavailable(
            "no HTTP fetcher was provided to the enumeration read")
    host = _ALCHEMY_HOSTS.get(chain)
    if not host:
        raise EnumerationUnavailable(
            f"chain {chain!r} has no pinned enumeration endpoint")
    url = (f"https://{host}/nft/v3/{key}/getNFTsForOwner"
           f"?owner={_norm_addr(address, 'address')}&withMetadata=true&pageSize=100"
           + "".join(f"&contractAddresses[]={_norm_addr(c, 'contract')}" for c in contracts))
    payload = fetch(url)
    out: List[Dict[str, Any]] = []
    for item in (payload or {}).get("ownedNfts", []) or []:
        contract = ((item.get("contract") or {}).get("address") or "").lower()
        if not contract:
            continue
        std = str((item.get("contract") or {}).get("tokenType") or "").lower()
        out.append({
            "contract": contract,
            "token_id": item.get("tokenId"),
            "standard": ERC1155 if "1155" in std else (ERC721 if "721" in std else None),
            "name": (item.get("name")
                     or (item.get("contract") or {}).get("name")),
            "balance": item.get("balance"),
        })
    return out


#: Alchemy hosts for the chains this rail reads. Absent = no enumeration, said
#: plainly, rather than a URL guessed from a chain name.
_ALCHEMY_HOSTS = {
    "ethereum": "eth-mainnet.g.alchemy.com",
    "base": "base-mainnet.g.alchemy.com",
    "arbitrum": "arb-mainnet.g.alchemy.com",
    "optimism": "opt-mainnet.g.alchemy.com",
    "polygon": "polygon-mainnet.g.alchemy.com",
    "bnb": "bnb-mainnet.g.alchemy.com",   # order 0015: getNFTsForOwner answered 200 (2026-11-11)
    # E-S1-12: getNFTsForOwner answered on this host (2026-11-08) with a contract
    # that has code on the Robinhood public RPC (eth_getCode, chain 4663).
    "robinhood": "robinhood-mainnet.g.alchemy.com",
}


def nft_transfer_card_context(params: dict, *, rpc=None) -> list:
    """124 D2: the owner's card for ``defi_trade_nft_transfer`` says whether the NFT has an
    ERC-6551 account and what it holds in the native unit — everything in that account leaves
    with the NFT. An unreadable fact is said ON the card, never left out."""
    from core.wallet import collection_registry, nft_rules
    chain = str(params.get("chain") or "")
    contract = str(params.get("contract") or "")
    if str(params.get("standard") or "erc721").lower() != "erc721" or not contract:
        return []
    try:
        token_id = int(params.get("token_id"))
        if rpc is None:
            from core.wallet.simulation import _default_rpc_for
            rpc = _default_rpc_for(chain)
        chain_id = collection_registry.chain_id_of(chain)
        accounts = nft_rules.account_addresses(
            chain_id, contract, [token_id], nft_rules.implementations(rpc, chain_id, contract))
        lines = []
        for account in sorted(accounts):
            code = rpc("eth_getCode", [account, "latest"])
            if not (isinstance(code, str) and len(code) > 2):
                continue
            wei = int(str(rpc("eth_getBalance", [account, "latest"]) or "0x0"), 16)
            lines.append(f"• This NFT has an ERC-6551 account {account}: {wei / 10 ** 18:.6f} "
                         f"native, and any tokens it holds (agent_nft_inspect lists them) — "
                         f"everything in it leaves with the NFT.")
    except Exception as exc:  # noqa: BLE001 — unreadable is not empty
        return [f"⚠️ ACCOUNT UNREADABLE ({type(exc).__name__}: {str(exc)[:160]}) — you would "
                f"approve without seeing what may leave with this NFT."]
    return lines or ["• This NFT has no deployed ERC-6551 account: nothing else leaves with it."]

