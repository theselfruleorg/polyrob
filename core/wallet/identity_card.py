"""The agent's card on an NFT account's ERC-8004 identity — the guard's half of every
registration THROUGH a token-bound account (proposal 115 W6, finding G3).

An ERC-6551P collection registers an identity for every minted token's account at mint: the
account owns the agentId, so only the account may change its registration file. The agent
publishes its card by signing ``account.execute(identityRegistry, 0, setAgentURI(agentId, uri),
0)`` (``agent_nft_bind_identity``; a ``register`` when the account has no identity yet).

The contract of record (the ERC-8004 reference ``IdentityRegistryUpgradeable``, the registry the
collection pins; the desk fork test ``test/ForkAgentHolder4663.t.sol`` drives the same calls):

* ``setAgentURI(uint256 agentId, string newURI)`` — the caller must be the agentId's owner or an
  operator of it (an approval); it sets the token URI and emits ``MetadataUpdate(agentId)``
  (ERC-4906) and ``URIUpdated(agentId, newURI, msg.sender)``. Moves nothing.
* ``setMetadata(uint256, string, bytes)`` — the journal anchor's write (``MetadataSet``).
* ``register()`` / ``register(string)`` / ``register(string,(string,bytes)[])`` — mint an identity
  to the caller.

The guard, from ``core/wallet/tx_guard_steps/`` (``tx_guard.py`` gains no line):

* shape (step 3, no RPC, :func:`shape_refusal`): through an account the pinned identity
  registry is called only by a registration intent, and a registration through an account is
  one of the calls above (a ``register`` overload when it mints, ``setAgentURI`` /
  ``setMetadata`` when it updates); ``setAgentURI`` carries no value and canonical calldata;
* pins (step 4, :func:`runtime_refusal`): the registry's ``ownerOf(agentId)`` is the account,
  and when the account's collection names identities (``hasIdentity`` / ``agentIdOf``, an
  ERC-6551P kind) and this token has one, the agentId is that one;
* measured (step 6f, :func:`assert_simulation`): nothing of the account moves or is approved,
  every log comes from the registry, exactly one ``URIUpdated(agentId, uri, account)`` with the
  signed URI, and ``MetadataUpdate`` only for that agentId.

The sale hold (``tx_guard_account._sale_hold``) applies as to every account act: it is an
account call, so it ends the sale readiness of a ready ERC-6551P account.

Each selector and topic is a literal; ``tests/unit/core/wallet/test_identity_card.py`` computes
every one from its signature. ⚠️ The signer re-runs the guard: no module-level wallet import.
"""
from __future__ import annotations

from typing import Optional, Tuple

SET_AGENT_URI_SIGNATURE = "setAgentURI(uint256,string)"
SET_AGENT_URI_SELECTOR = "0x0af28bd3"
SET_METADATA_SELECTOR = "0x466648da"        # setMetadata(uint256,string,bytes)
#: register() / register(string) / register(string,(string,bytes)[])
REGISTER_SELECTORS = ("0x1aa3a008", "0xf2c298be", "0x8ea42286")
TOKEN_URI_SELECTOR = "0xc87b56dd"           # tokenURI(uint256)
TOPIC_URI_UPDATED = "0x3a2c7fffc2cba7582c690e3b82c453ea02a308326a98a3ad7576c606336409fb"  # URIUpdated(uint256,string,address)
TOPIC_METADATA_UPDATE = "0xf8e1a15aba9398e019f0b49df1a4fde98ee17ae345cb5f6b5e2c27f5033e8ce7"  # MetadataUpdate(uint256)

#: The verb that makes these calls (named in a refusal).
CARD_VERB = "agent_nft_bind_identity"


def _word(address: str) -> str:
    return "0x" + "0" * 24 + str(address).lower().removeprefix("0x")


def decode_set_agent_uri(data: str) -> Tuple[int, str]:
    """``(agentId, uri)`` of ``setAgentURI`` calldata. Raises ValueError unless *data* is that
    call and re-encodes to the same bytes (no trailing or odd words)."""
    from core.wallet import abi
    text = str(data or "").lower()
    if not text.startswith(SET_AGENT_URI_SELECTOR):
        raise ValueError("the calldata is not setAgentURI(uint256,string)")
    types = [{"type": "uint256"}, {"type": "string"}]
    agent_id, uri = abi.decode(types, "0x" + text[10:])
    if abi.encode_call("setAgentURI", types, [int(agent_id), str(uri)]).lower() != text:
        raise ValueError("the setAgentURI calldata is not its canonical encoding")
    return int(agent_id), str(uri)


def agent_id_of(rpc, collection: str, token_id: int) -> Optional[int]:
    """The agentId the collection registered for *token_id* at mint (``hasIdentity`` /
    ``agentIdOf``, ERC-6551P collections), or None when it has none. Raises when the collection
    does not answer (a collection without these views). Re-exported by
    ``core.wallet.erc8004_reputation``."""
    from core.wallet import abi
    u256 = [{"type": "uint256"}]

    def call(name, outputs):
        raw = rpc("eth_call", [{"to": collection, "data": abi.encode_call(
            name, u256, [int(token_id)])}, "latest"])
        if raw in (None, "", "0x"):
            raise ValueError(f"{name}() on {collection} returned no data")
        return abi.decode(outputs, raw)[0]
    if not call("hasIdentity", [{"type": "bool"}]):
        return None
    return int(call("agentIdOf", u256))


def _registry(chain) -> Optional[str]:
    from core.wallet import erc8004
    try:
        return erc8004.resolve_identity_registry(chain).lower()
    except Exception:  # noqa: BLE001 — no pinned registry on this chain
        return None


def _is_card(intent, tx) -> bool:
    return bool(getattr(intent, "via_account", None) and intent.is_registration
                and not intent.expects_mint
                and str((tx or {}).get("data") or "").lower().startswith(SET_AGENT_URI_SELECTOR))


def shape_refusal(intent, tx) -> Optional[str]:
    """Step 3 (no RPC). *tx* is the INNER call under ``via_account``. None = no objection."""
    if not getattr(intent, "via_account", None):
        return None
    to = str((tx or {}).get("to") or "").lower()
    registry = _registry(intent.chain)
    if not intent.is_registration:
        if registry and to == registry:
            return (f"through an NFT account the pinned ERC-8004 identity registry {registry} is "
                    f"called only as a registration ({CARD_VERB}): its identity token is the "
                    f"agent's, and moving or approving it ends the account's agentWallet for good")
        return None
    data = str((tx or {}).get("data") or "").lower()
    sel = data[:10]
    allowed = REGISTER_SELECTORS if intent.expects_mint else (SET_AGENT_URI_SELECTOR,
                                                              SET_METADATA_SELECTOR)
    if sel not in allowed:
        what = "a register() overload" if intent.expects_mint else "setAgentURI or setMetadata"
        return (f"a registration through an NFT account calls {what} on the identity registry; "
                f"this one calls {sel or 'nothing'}")
    if sel == SET_AGENT_URI_SELECTOR:
        try:
            decode_set_agent_uri(data)
            value = int((tx or {}).get("value") or 0)
        except (TypeError, ValueError) as exc:
            return f"the setAgentURI call through the account is malformed ({exc})"
        if value:
            return f"setAgentURI carries no value; this call sends {value} wei"
    return None


def runtime_refusal(intent, tx, rpc) -> Optional[str]:
    """Step 4: the agentId of a ``setAgentURI`` through an account is the account's own
    identity. Fail closed."""
    if not _is_card(intent, tx):
        return None
    from core.wallet import abi, collection_registry, erc6551
    account = str(intent.via_account)
    try:
        agent_id, _uri = decode_set_agent_uri((tx or {}).get("data"))
        registry = _registry(intent.chain)
        if registry is None:
            return f"no pinned ERC-8004 identity registry for {intent.chain}"
        _cid, collection, token_id = erc6551.read_token(rpc, account)
        profile = collection_registry.profile_for(
            collection_registry.chain_id_of(intent.chain), collection)
        version = profile.version_for_account(int(token_id), account)
        if version is not None and version.kind.collection_iface:
            own = agent_id_of(rpc, collection, int(token_id))
            if own is not None and int(own) != agent_id:
                return (f"setAgentURI names agentId {agent_id}, but the identity of {collection} "
                        f"#{token_id} is agentId {own} — the account publishes only its own card")
        raw = rpc("eth_call", [{"to": registry, "data": abi.encode_call(
            "ownerOf", [{"type": "uint256"}], [agent_id])}, "latest"])
        owner = "0x" + str(raw)[-40:]
        if not isinstance(raw, str) or len(raw) < 66 or owner.lower() != account.lower():
            return (f"agentId {agent_id} on the identity registry is owned by {owner}, not the "
                    f"account {account} — only its owner may publish its card")
    except Exception as exc:  # noqa: BLE001
        return f"the identity of the account could not be read ({exc}); failing closed"
    return None


def assert_simulation(intent, tx, deltas, holder: str) -> Optional[str]:
    """Step 6f: the measured result of a ``setAgentURI`` through the account (*holder*)."""
    if not _is_card(intent, tx):
        return None
    from core.wallet import abi
    agent_id, uri = decode_set_agent_uri((tx or {}).get("data"))
    registry = _registry(intent.chain)
    moved = [name for name, rows in (
        ("token transfers", getattr(deltas, "holder_transfers", ())),
        ("token approvals", getattr(deltas, "holder_approvals", ())),
        ("NFT moves out", getattr(deltas, "holder_nft_out", ())),
        ("NFT arrivals", getattr(deltas, "holder_nft_in", ())),
        ("operator approvals", getattr(deltas, "holder_operator_grants", ())),
        ("NFT approvals", getattr(deltas, "holder_nft_approvals", ())),
        ("Permit2 approvals", getattr(deltas, "holder_permit2_grants", ())),
        ("ERC-6909 approvals", getattr(deltas, "holder_6909_approvals", ()))) if rows]
    moved += [f"token {t} {d:+d}" for t, d in (getattr(deltas, "token_deltas", None) or {}).items() if d]
    if moved:
        return f"publishing the card moves or approves something ({', '.join(moved)})"
    seen = 0
    for log in getattr(deltas, "logs", None) or ():
        emitter = str(log.get("address") or "").lower()
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        head = topics[0] if topics else ""
        if emitter != registry:
            return (f"publishing the card makes {emitter} emit an event — every log of it comes "
                    f"from the identity registry {registry}")
        data = str(log.get("data") or "0x")
        if head == TOPIC_URI_UPDATED:
            try:
                (got,) = abi.decode([{"type": "string"}], data)
            except Exception:  # noqa: BLE001
                got = None
            if (len(topics) != 3 or int(topics[1], 16) != agent_id or topics[2] != _word(holder)
                    or got != uri):
                return (f"the registry's URIUpdated names another agentId, caller or URI "
                        f"({topics[1:3]}), not agentId {agent_id} set by the account {holder}")
            seen += 1
        elif head == TOPIC_METADATA_UPDATE:
            word = data.lower().removeprefix("0x")
            if len(word) != 64 or int(word, 16) != agent_id:
                return f"the registry's MetadataUpdate names another agentId (0x{word[:64]})"
        else:
            return (f"publishing the card emits an event that is not part of it "
                    f"({head[:10] or 'anonymous'})")
    if seen != 1:
        return (f"the simulation shows {seen} URIUpdated event(s) for agentId {agent_id} — "
                f"setAgentURI emits exactly one")
    return None


def read_token_uri(rpc, chain: str, agent_id: int) -> str:
    """The registry's ``tokenURI(agentId)``: the card the identity publishes. Raises when
    unreadable (never "no card")."""
    from core.wallet import abi, erc8004
    registry = erc8004.resolve_identity_registry(chain)
    raw = rpc("eth_call", [{"to": registry, "data": abi.encode_call(
        "tokenURI", [{"type": "uint256"}], [int(agent_id)])}, "latest"])
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 130:
        raise ValueError(f"tokenURI({agent_id}) returned no string")
    (uri,) = abi.decode([{"type": "string"}], raw)
    return str(uri)


__all__ = ["agent_id_of", "assert_simulation", "decode_set_agent_uri", "read_token_uri", "runtime_refusal",
           "shape_refusal"]
