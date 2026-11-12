"""The custom errors of a pinned collection, its sale and its sale rails — and which table a
reverted simulation is read against.

A 4-byte selector is not unique across contracts (``NotOwner()`` is a common error), so a
revert is named only from the tables of the contracts the transaction is KNOWN to reach:

* a call through an ERC-6551 account (``intent.via_account``): the account kind's own errors
  (``core.wallet.account_kinds``), and — for a kind whose collection is an ERC-6551P
  collection — the collection's;
* a collection mint or a claim from a pinned sale: the sale's errors, then the collection's
  (the sale mints through the collection);
* a reveal, or any call whose target is a pinned collection: the collection's;
* a call to a pinned collection's market or zone: the sale rails'.

Anything else is not named (the revert still refuses; only the words are missing).

The tables are the first deployment's sources (polyrob-desk ``contracts/src``). Each selector is
a literal; a unit test computes every one from its signature.
"""
from typing import Dict, List, Optional, Tuple

from core.wallet.custom_errors import ErrorTable, decode

#: A pinned ERC-6551P collection: the standard's kit (``kit/ERC6551PCollection.sol:40-47``),
#: the ERC-721 base (``kit/AccountedERC721.sol:32``), the identity module
#: (``kit/modules/AgentIdentity.sol:30``) and the first deployment's own collection errors
#: (``src/Polyrob.sol:93-100``).
COLLECTION_ERRORS: Dict[str, Tuple[str, str]] = {
    "0xb1d04f08": ("NonexistentToken()", "the token id does not exist"),
    "0x620e721c": ("SaleNotReady()", "a plain marketplace fill needs readyToSell from an "
                                     "earlier block"),
    "0xd15af9b5": ("AccountInBatch()", "the NFT cannot move while its account runs a batch"),
    "0xc74ed4fa": ("SellerIsAccount()", "an ERC-6551 account may not be the seller"),
    "0xb3077bb1": ("NotTicketOpener()", "only the market or zone that opened the sale ticket "
                                        "may use it"),
    "0xd9e1a63a": ("TicketBusy()", "a sale ticket for this NFT is already open"),
    "0x68da804f": ("NotTokenAccount()", "only the token's own account may make this call"),
    "0x59dc379f": ("NotTokenOwner()", "only the NFT's owner may make this call"),
    "0xb79e3f3f": ("OwnershipCycle()", "an NFT may not go into its own account, nor into an "
                                       "account of the same collection"),
    "0x9bb6f794": ("IdentityGas()", "too little gas left to register the token's identity"),
    "0xb2c9bc88": ("NotRevealed()", "the token is not revealed yet"),
    "0xc2b03beb": ("RoyaltyTooHigh()", "the royalty is above the collection's cap"),
    "0x89051165": ("RenounceDisabled()", "the collection's ownership cannot be renounced"),
    "0x7953be88": ("NotFaceWriter()", "only the face writer may record a face"),
    "0xc71e016b": ("FaceWriterIsStopped()", "the face writer is stopped"),
    "0x26f30ab0": ("FaceAlreadyRecorded(uint256 id)", "this token's face is already recorded"),
    "0xc7d2c7fd": ("BadFaceRecord(uint256 id)", "the face record does not match the token"),
    "0xa7283fb9": ("NotSale()", "only the collection's SALE() may mint — mint through the "
                                "pinned sale"),
}

#: The pinned sale, the collection's only public mint (the first deployment's
#: ``src/drop/PolyrobSale.sol:84-104``).
SALE_ERRORS: Dict[str, Tuple[str, str]] = {
    "0xbca98e9f": ("NotCollection()", "only the collection may make this call"),
    "0x30cd7471": ("NotOwner()", "only the sale's owner may make this call"),
    "0x2d698557": ("BadRoot()", "the whitelist root is not valid"),
    "0xfe829257": ("ConfigFrozen()", "the sale's configuration is frozen"),
    "0xd311bc39": ("NotConfigured()", "the sale is not configured yet"),
    "0x589ed34b": ("MintClosed()", "the mint is closed"),
    "0x33eb4859": ("NotWLPhase()", "the whitelist phase is over (or not open): use mint"),
    "0x275b660b": ("NotPublicPhase()", "the public phase is not open: in the whitelist phase "
                                       "use mintWL"),
    "0x665c1c57": ("NotListed()", "this treasury and allowance are not on the whitelist (the "
                                  "proof does not match)"),
    "0x454c0f81": ("OverAllowance()", "the whitelist allowance is used up"),
    "0x12936432": ("BadQty()", "the quantity is outside what one call may mint"),
    "0x52df9fe5": ("SoldOut()", "the collection is sold out"),
    "0x51481720": ("WrongPrice(uint256 expected)", "the value is not what the sale asks now"),
    "0xe0f0d3cc": ("TooMuch(uint256 max)", "the value is above what the sale takes"),
    "0xba329a9b": ("NotSettled()", "the sale is not settled yet"),
    "0x005e7936": ("TeamCapReached()", "the team allocation is used up"),
    "0x00defe30": ("BadRecord()", "the record is not one this call may settle"),
    "0xe67eb3cf": ("PushGas()", "too little gas left to push a refund"),
    "0x797dba54": ("NothingOwed()", "the sale owes this address nothing"),
    "0x360e42e1": ("ClaimFailed()", "the claim payment failed"),
    "0x750b219c": ("WithdrawFailed()", "the withdrawal failed"),
}

#: The pinned sale rails of an ERC-6551P collection (the first deployment's sources): its market
#: (``src/sale/PolyrobMarket.sol:102-119``, with ``src/sale/Conditions.sol:39-42``) and its zone
#: (``src/sale/PolyrobZone.sol:37-43``).
SALE_RAIL_ERRORS: Dict[str, Tuple[str, str]] = {
    "0xb5dfd9e5": ("Reentered()", "the market is already inside a call"),
    "0xdb1047ce": ("NotThisMarket()", "the order names another market"),
    "0x203d82d8": ("Expired()", "the order has expired"),
    "0x4bd574ec": ("BadNonce()", "the order's nonce is not the signer's current nonce"),
    "0x206c6b65": ("SaltUsed()", "the order's salt is already used"),
    "0x5cd5d233": ("BadSignature()", "the order's signature is not valid"),
    "0x7f9f633e": ("BadPromise()", "the seller's promise does not match the account"),
    "0x5ec82351": ("NotSeller()", "the seller no longer owns the token"),
    "0x4b19d294": ("StaleListing()", "the token moved since the listing was signed"),
    "0xc74ed4fa": ("SellerIsAccount()", "an ERC-6551 account may not be the seller"),
    "0x788a686f": ("WrongPayment()", "the payment is not what the order asks"),
    "0x175d0177": ("WrongId()", "the token id is not the one the order names"),
    "0xb707f3b3": ("NotConsumed()", "the sale ticket was not consumed by the transfer"),
    "0x2a2f45bc": ("NotDelivered()", "the token did not reach the buyer"),
    "0x98c2c368": ("BadOrder()", "the order is not one this rail fills"),
    "0xf8b63692": ("EthOnlyFromSeaport()", "native value is accepted only from Seaport"),
    "0x360e42e1": ("ClaimFailed()", "the claim payment failed"),
    "0x797dba54": ("NothingOwed()", "the market owes this address nothing"),
    "0x6f45aa49": ("NoBuyerConditions()", "a fill needs the buyer's conditions"),
    "0x72fc940b": ("ConditionFailed(uint256 index)", "a buyer condition on the account's "
                                                     "holdings failed at the fill"),
    "0x1c2b0777": ("BadAccount()", "the conditions name another account"),
    "0x7ae08eb7": ("CondGas()", "too little gas left to check the conditions"),
    "0x3786ddc3": ("NotSeaport()", "only Seaport may call the zone"),
    "0xc5d68203": ("BadZoneHash()", "the order's zone hash does not match"),
    "0xfea7e897": ("NotThisZone()", "the order names another zone"),
}


def _account_tables(intent, rpc) -> List[Tuple[str, ErrorTable]]:
    """The kind's errors (and its collection's) for a call through ``intent.via_account``.
    The pre-flight's own reads (the clone's implementation, the account's ``token()``) find
    the pinned version; unreadable = no name."""
    from core.wallet import collection_registry, erc6551
    impl = erc6551.read_implementation(rpc, intent.via_account)
    chain_id, contract, _token_id = erc6551.read_token(rpc, intent.via_account)
    profile = collection_registry.profile_for(int(chain_id), contract)
    version = profile.account_version(impl) if profile is not None else None
    if version is None:
        return []
    out = [(f"the {version.kind.label}", version.kind.errors)]
    if version.kind.collection_iface:
        out.append(("the pinned collection", COLLECTION_ERRORS))
    return out


def _target_tables(chain: str, target: str) -> List[Tuple[str, ErrorTable]]:
    from core.wallet import collection_registry
    t = str(target or "").lower()
    if not t:
        return []
    for p in collection_registry.profiles_on(collection_registry.chain_id_of(chain)):
        if p.address == t:
            return [("the pinned collection", COLLECTION_ERRORS)]
        if p.sale is not None and p.sale.address == t:
            return [("the pinned sale", SALE_ERRORS), ("the pinned collection", COLLECTION_ERRORS)]
        std = p.erc6551p
        if std is not None and t in (std.market, std.zone):
            return [("the pinned sale rail", SALE_RAIL_ERRORS)]
    return []


def revert_name(intent, tx: Optional[dict], revert_data, *, rpc=None) -> Optional[str]:
    """``"<who> refused it on chain: Name(args) (text)"`` for a reverted simulation, or None.

    *tx* is the call the guard judged (the INNER call under ``via_account``). Never raises:
    a name is a courtesy; the refusal stands without it."""
    try:
        tables: List[Tuple[str, ErrorTable]] = []
        if getattr(intent, "via_account", None):
            from core.wallet.simulation import _default_rpc_for
            tables += _account_tables(intent, rpc or _default_rpc_for(intent.chain))
        if getattr(intent, "is_collection_mint", False):
            tables += [("the pinned sale", SALE_ERRORS), ("the pinned collection", COLLECTION_ERRORS)]
        tables += _target_tables(intent.chain, (tx or {}).get("to"))
        for who, table in tables:
            named = decode(revert_data, table)
            if named:
                return f"{who} refused it on chain: {named}"
    except Exception:  # noqa: BLE001 — no name; the revert still refuses
        return None
    return None


__all__ = ["COLLECTION_ERRORS", "SALE_ERRORS", "SALE_RAIL_ERRORS", "revert_name"]
