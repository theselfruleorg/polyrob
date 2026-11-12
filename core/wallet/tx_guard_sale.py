"""tx_guard: the BUYER's calls on a pinned market of an ERC-6551P collection (proposal 111 §8.1
``buy``, ``fill``, ``claim_owed``; 115 W9) — the ``tx_guard_account.py`` pattern.

The guard stays the one authorizer: ``tx_guard_steps.shape`` calls :func:`gate` before any RPC
and :func:`runtime_refusal` with the RPC pins, ``tx_guard_steps.measure`` passes
:func:`views` / :func:`watch_native` to the simulation and calls :func:`assert_simulation`
after the generic delta checks. ``tx_guard_account.sale_act_gate`` (the seller's acts) leaves
the buyer's selectors to THIS gate only; every other call that names a pinned market refuses
there.

* ``buy`` — ``market.buy(listing, sig, sellerPromise, buyerConditions, recipient)`` signed by
  the treasury itself, declared as a NATIVE send of exactly ``listing.price`` to the market
  (``token=None``, ``amount_raw == value == price``) with ``expected_nft_in`` = that one token.
  The listing is the pinned collection's, priced in ETH (an ERC-20 listing is not built), the
  recipient is the treasury, and ``buyerConditions`` are EXACTLY what
  ``core.wallet.sale_conditions`` builds from a fresh read of the account now (less only
  holdings whose value is not checked anyway) — the guard re-reads, it never trusts the caller.
  Measured: the treasury's native out is the price (the generic native branch), exactly ONE NFT
  arrives — that id, from the seller —, one ``Sold(collection, id, seller | treasury, ETH,
  price, royalty ≤ maxRoyalty)`` from the market, ``ownerOf`` seller → treasury and the salt
  used (views before/after), and the market's own native balance moves only by the ``Owed`` it
  books (it holds nothing else after).
* ``claim_owed`` — ``market.claim(treasury)`` on the CLAIM shape (``is_claim`` +
  ``min_native_inflow_wei``; the market's ``owed(treasury)`` read before, 0 after, the market
  pays exactly that, nothing else of the treasury moves). The market emits no event for it.
* ``accept`` (an offer, the SELLER's act; 111 §8.1, W9a) — ``market.accept(offer, sig, id,
  buyerConditions, sellerPromise = [])`` signed by the treasury, declared as an NFT operation
  whose ONLY declared move is that token out (``nft_out``), no value. The offer names the
  pinned collection, an ERC-20 currency on core's reviewed list, the id (or ``anyId``), and the
  buyer's conditions hash to its ``conditionsHash``; the market's own ``offerDigest`` equals
  core's; the market holds the single-token approval of the id (``agent_nft_sale_list`` /
  ``agent_nft_sale_accept`` make it first). Measured: exactly that token out to
  ``offer.buyer``, one ``Sold(collection, id, treasury, buyer, currency, price, royalty ≤
  maxRoyalty)``, the treasury's ``balanceOf`` in the currency up by exactly ``price − royalty``,
  the market's own balance of it and its ETH unchanged, the offer's salt used, and
  ``getApproved(id)`` 0 after (the transfer consumed the approval). Nothing else of the treasury
  moves or is approved.
* ``checkedFill`` — refused by name: not built in this wave.

Imports: only leaf modules (``sale_conditions``, ``sale_orders``, ``abi``). The registry and the profile come in
as arguments, so this module stays out of the guard's import cycle. ⚠️ The signer re-runs this
code in its own process: no new module-level imports.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

KIND_BUY = "buy"
KIND_CLAIM = "claim_owed"
KIND_ACCEPT = "accept"

#: The verbs that may make these calls (named in a refusal).
BUYER_VERBS = "agent_nft_buy and agent_nft_market_claim"
ACCEPT_VERB = "agent_nft_sale_accept"

#: The market selectors this gate owns (``tx_guard_account.sale_act_gate`` leaves them here).
#: Literals of ``sale_conditions`` (buy, claim, checkedFill, accept).
BUYER_SELECTORS = ("0xe9c3b815", "0x1e83409a", "0xec48f34c", "0x8e431f0c")

NOT_BUILT_FILL = ("checkedFill is not built in this wave: its result (one token in from the "
                  "market, the change back, the market holding nothing, CheckedFill) cannot be "
                  "proven without Seaport 1.6 on the proof rig; only market listings are bought "
                  "(agent_nft_buy)")


@dataclass(frozen=True)
class BuyerAct:
    kind: str
    market: str
    profile: object
    call: object = None   # sale_conditions.BuyCall for a buy, sale_orders.AcceptCall for an accept


def _flags(intent):
    return [name for name, on in (
        ("a deployment", intent.is_deploy), ("an NFT operation", intent.is_nft_op),
        ("a registration", intent.is_registration),
        ("an allowance operation", intent.is_allowance_op),
        ("a liquidity operation", intent.is_liquidity_op),
        ("a collection reveal", intent.is_collection_reveal),
        ("a collection mint", intent.is_collection_mint),
        ("a resource call", intent.is_resource_call), ("a journal entry", intent.is_journal_entry),
        ("an account call (via_account)", bool(intent.via_account)),
        ("an approve-spend-reset batch", intent.via_account_batch),
        ("a transfer", intent.is_transfer)) if on]


def _declared(intent, *names):
    return [name for name, v in (
        ("token", intent.token), ("inflow_token", intent.inflow_token),
        ("min_inflow_raw", intent.min_inflow_raw),
        ("min_native_inflow_wei", intent.min_native_inflow_wei),
        ("allowance grants", intent.expected_allowance_grants),
        ("nft_out", intent.nft_out), ("expected_nft_in", intent.expected_nft_in),
        ("nft_operator_ops", intent.nft_operator_ops),
        ("nft_approval_revokes", intent.nft_approval_revokes),
        ("erc6909_revokes", intent.erc6909_revokes), ("permit2_revokes", intent.permit2_revokes),
        ("mint_ids", intent.mint_ids), ("reveal_ids", intent.reveal_ids),
        ("expected_events", intent.expected_events),
        ("amount_raw", intent.amount_raw)) if v and name not in names]


def gate(intent, tx, holder: str, markets) -> Tuple[Optional[BuyerAct], Optional[str]]:
    """``(act, None)`` for a well-formed buyer call, ``(None, None)`` for an intent that names no
    pinned market with a buyer selector, ``(None, why)`` to refuse. *markets* is ``{market:
    profile}`` of the chain's pinned profiles. No RPC."""
    import core.wallet.sale_conditions as S
    to_tx = str((tx or {}).get("to") or "").lower()
    to_i = str(intent.to or "").lower()
    data = str((tx or {}).get("data") or "").lower()
    hit = next((a for a in (to_tx, to_i) if a and a in markets), None)
    if hit is None or data[:10] not in BUYER_SELECTORS:
        return None, None
    where = f"the pinned market {hit}"
    if data[:10] == S.CHECKED_FILL_SELECTOR:
        return None, f"{where}: {NOT_BUILT_FILL}"
    if to_i != hit or to_tx != hit:
        return None, (f"a call on {where} must be addressed to it in the intent ({intent.to}) "
                      f"and in the signed transaction ({(tx or {}).get('to')})")
    other = _flags(intent)
    try:
        value = int((tx or {}).get("value") or 0)
    except (TypeError, ValueError):
        return None, "the transaction value is unreadable"
    profile = markets[hit]
    me = str(holder or "").lower()
    if data[:10] == S.ACCEPT_SELECTOR:
        return _accept_gate(intent, data, value, me, hit, profile, where)
    if data[:10] == S.CLAIM_SELECTOR:
        if not intent.is_claim or other:
            return None, (f"claim(address) on {where} is the claim of what the market owes: "
                          f"only the guarded {BUYER_VERBS} make it, declared as a claim "
                          f"({', '.join(other) or 'not declared is_claim'})")
        extra = _declared(intent, "min_native_inflow_wei")
        if extra:
            return None, (f"a claim from {where} receives native value only, but this intent "
                          f"declares {', '.join(extra)}")
        if not (intent.min_native_inflow_wei or 0) > 0:
            return None, (f"a claim from {where} must declare the native it receives "
                          f"(min_native_inflow_wei > 0, read from owed())")
        if value:
            return None, f"a claim carries no value; this transaction sends {value} wei"
        try:
            who = S.decode_claim(data)
        except ValueError as exc:
            return None, f"{where}: {exc}"
        if who != me:
            return None, (f"the claim pays {who}, not the signing treasury {holder} — the guard "
                          f"measures what arrives in the signing wallet only")
        return BuyerAct(KIND_CLAIM, hit, profile), None
    # buy
    if intent.is_claim or other:
        return None, (f"buy on {where} is made only by the guarded {BUYER_VERBS}, declared as a "
                      f"native payment with the NFT that arrives (this intent: "
                      f"{', '.join(other) or 'a claim'})")
    try:
        call = S.decode_buy(data)
    except ValueError as exc:
        return None, f"{where}: {exc}"
    l = call.listing
    if l.collection != profile.address:
        return None, (f"the listing sells {l.collection} #{l.id}, not the pinned collection "
                      f"{profile.address} of {where}")
    if l.currency != S.ZERO:
        return None, (f"the listing is priced in the token {l.currency}: this build buys ETH "
                      f"listings only (an ERC-20 price needs an approve-buy-reset batch, not "
                      f"built)")
    if intent.token is not None or intent.inflow_token or intent.min_inflow_raw \
            or intent.min_native_inflow_wei:
        return None, "a buy is declared as a native payment (token=None) with no other inflow"
    extra = _declared(intent, "amount_raw", "expected_nft_in")
    if extra:
        return None, f"a buy declares only its price and the NFT that arrives, not {', '.join(extra)}"
    want = ((l.collection, "erc721", int(l.id), 1),)
    got = tuple((str(c).lower(), str(s).lower(), int(i), int(a))
                for (c, s, i, a) in (intent.expected_nft_in or ()))
    if got != want:
        return None, (f"a buy must declare exactly the NFT that arrives, {want[0]}; this intent "
                      f"declares {list(got)}")
    if call.recipient != me:
        return None, (f"the buy sends the NFT to {call.recipient}, not to the signing treasury "
                      f"{holder} — the guard measures what arrives in the signing wallet only")
    if l.seller == me:
        return None, "the treasury is the listing's seller — a buy from itself"
    if not (int(intent.amount_raw) == value == int(l.price)) or value <= 0:
        return None, (f"the buy pays {value} wei and declares {intent.amount_raw}, but the "
                      f"listing's price is {l.price} — all three must be the same")
    if not call.conditions:
        return None, "the buy carries no buyer condition (the market refuses: NoBuyerConditions)"
    if S.conditions_hash(call.promise) != l.promiseHash:
        return None, "the seller's promise in the call does not hash to the listing's promiseHash"
    return BuyerAct(KIND_BUY, hit, profile, call), None


def _accept_gate(intent, data: str, value: int, me: str, hit: str, profile, where: str):
    """The shape of the seller's ``accept`` (no RPC)."""
    import core.wallet.sale_conditions as S
    import core.wallet.sale_orders as O
    other = [f for f in _flags(intent) if f != "an NFT operation"]
    if not intent.is_nft_op or other or intent.is_claim:
        return None, (f"accept on {where} is made only by the guarded {ACCEPT_VERB}, declared as "
                      f"an NFT operation whose one move is the sold token "
                      f"({', '.join(other) or 'not declared is_nft_op'})")
    extra = _declared(intent, "nft_out")
    if extra or intent.amount_raw:
        return None, (f"an accept declares only the token that leaves, not "
                      f"{', '.join(extra) or 'an amount'}")
    if value:
        return None, f"an accept carries no value; this transaction sends {value} wei"
    try:
        call = O.decode_accept(data)
    except ValueError as exc:
        return None, f"{where}: {exc}"
    o = call.offer
    if o.collection != profile.address:
        return None, (f"the offer is for {o.collection}, not the pinned collection "
                      f"{profile.address} of {where}")
    if o.currency == S.ZERO:
        return None, "the offer is priced in ETH; the market takes offers in an ERC-20 only"
    want = ((profile.address, "erc721", int(call.token_id), 1),)
    got = tuple((str(c).lower(), str(s).lower(), int(i), int(a))
                for (c, s, i, a) in (intent.nft_out or ()))
    if got != want:
        return None, (f"an accept must declare exactly the token that leaves, {want[0]}; this "
                      f"intent declares {list(got)}")
    if not o.anyId and int(o.id) != int(call.token_id):
        return None, f"the offer is for #{o.id}, not #{call.token_id} (and not for any id)"
    if call.promise:
        return None, ("the accept carries a seller's promise: this build accepts with none (the "
                      "buyer's own conditions are what the market checks for the buyer)")
    if not call.conditions:
        return None, "the accept carries no buyer condition (the market refuses: NoBuyerConditions)"
    if S.conditions_hash(call.conditions) != o.conditionsHash:
        return None, "the buyer's conditions in the call do not hash to the offer's conditionsHash"
    if o.buyer == me:
        return None, "the treasury is the offer's buyer — an accept of its own offer"
    if not 0 < o.price < 1 << 128 or o.maxRoyalty > o.price:
        return None, f"the offer's price {o.price} / maxRoyalty {o.maxRoyalty} is not a price"
    return BuyerAct(KIND_ACCEPT, hit, profile, call), None


def _accept_runtime(act: BuyerAct, intent, holder: str, rpc) -> Optional[str]:
    """The live facts of an accept (fail closed in the caller)."""
    import core.wallet.sale_acts as A
    import core.wallet.sale_conditions as S
    import core.wallet.sale_orders as O
    call, profile = act.call, act.profile
    o, tid, me = call.offer, int(call.token_id), str(holder).lower()
    reviewed = {str(t).lower() for t in S.read_context(intent.chain).reviewed_erc20}
    if o.currency not in reviewed:
        return f"the offer pays in {o.currency}, not a token on core's reviewed list"
    _account, version = profile.account_of(tid)
    if not getattr(version.kind, "sale_ready_view", None):
        return f"#{tid} of {profile.address} is held in {version.kind.label}: no sale rule"

    def call_(to, data):
        return rpc("eth_call", [{"to": to, "data": data}, "latest"])
    owner = "0x" + format(S._word_of(call_(profile.address, S.encode_owner_of(tid))), "064x")[-40:]
    if owner != me:
        return f"this treasury does not own #{tid} (its owner is {owner})"
    market_of = "0x" + format(S._word_of(call_(profile.address, O.MARKET_SELECTOR)), "064x")[-40:]
    if market_of != act.market:
        return f"the collection's MARKET() is {market_of}, not the pinned market {act.market}"
    if A.read_approved(rpc, profile.address, tid) != act.market:
        return (f"the pinned market is not approved for #{tid}: the accept makes the single-token "
                f"approval first (agent_nft_sale_accept), never setApprovalForAll")
    now = int((rpc("eth_getBlockByNumber", ["latest", False]) or {}).get("timestamp"), 16)
    if now > o.expiry:
        return f"the offer expired (at {o.expiry}, now {now})"
    if A.read_nonce(rpc, act.market, o.buyer) != o.nonce:
        return "the buyer cancelled every market order (its nonce moved)"
    if A.read_used_salt(rpc, act.market, o.buyer, o.salt):
        return "the buyer cancelled this offer, or it was used (its salt is used)"
    name, version_s, cid, verifying = O.read_domain(rpc, act.market)
    if verifying != act.market or int(cid) != int(profile.chain_id):
        return "the pinned market's eip712Domain() names another contract or chain"
    mine = O.offer_digest(o, name=name, version=version_s, chain_id=cid, market=act.market)
    theirs = "0x" + format(S._word_of(call_(act.market, O.encode_offer_digest(o))), "064x")
    if mine != theirs:
        return "core and the pinned market disagree on the offer's EIP-712 digest"
    return None


def runtime_refusal(act: BuyerAct, intent, tx, holder: str, rpc, registry) -> Optional[str]:
    """The RPC pins of a buyer call: the collection's and the market's code against their pins;
    for a claim the market owes at least the declared minimum; for a buy the token's account is
    of a kind with a sale rule, and the calldata's conditions are the ones a FRESH read of the
    account builds now (t = 0 or 50 bps). *registry* is ``core.wallet.collection_registry``.
    Fail closed."""
    import core.wallet.sale_conditions as S
    profile, std = act.profile, act.profile.erc6551p
    try:
        why = (registry.runtime_refusal(rpc, profile)
               or registry.code_pin_refusal(rpc, std.market, std.market_runtime_sha256,
                                            "pinned market"))
        if why:
            return why
        if act.kind == KIND_ACCEPT:
            return _accept_runtime(act, intent, holder, rpc)
        if act.kind == KIND_CLAIM:
            raw = rpc("eth_call", [{"to": act.market, "data": S.encode_owed(holder)}, "latest"])
            owed = int(str(raw)[2:66], 16)
            if owed < int(intent.min_native_inflow_wei or 0):
                return (f"the market owes this treasury {owed} wei, less than the declared "
                        f"{intent.min_native_inflow_wei}")
            return None
        l = act.call.listing
        account, version = profile.account_of(int(l.id))
        if not getattr(version.kind, "sale_ready_view", None):
            return (f"#{l.id} of {l.collection} is held in {version.kind.label}: no sale rule, "
                    f"so the market's conditions cannot protect the buyer")
        ctx = S.read_context(intent.chain)
        reasons = []
        for bps in S.TOLERANCE_BPS:
            _snap, built = S.read_and_build(rpc, collection=l.collection, token_id=int(l.id),
                                            account=account, deploy_block=profile.deploy_block,
                                            ctx=ctx, tolerance_bps=bps)
            reason = S.admissible(act.call.conditions, built)
            if reason is None:
                return None
            reasons.append(reason)
        return (f"the buyer conditions in the transaction are not the ones a fresh read of the "
                f"account {account} builds now ({reasons[0]}) — the account changed since the "
                f"quote (quote again), or the list was not built by core")
    except Exception as exc:  # noqa: BLE001
        return f"the market call's pins could not be read ({exc}); failing closed"


def views(act: BuyerAct, holder: str) -> Tuple[Tuple[str, str], ...]:
    """The ``(to, data)`` views the simulation reads before and after."""
    import core.wallet.sale_acts as A
    import core.wallet.sale_conditions as S
    if act.kind == KIND_CLAIM:
        return ((act.market, S.encode_owed(holder)),)
    if act.kind == KIND_ACCEPT:
        import core.wallet.sale_orders as O
        o, tid = act.call.offer, int(act.call.token_id)
        return ((o.collection, S.encode_owner_of(tid)), (o.collection, O.encode_get_approved(tid)),
                (act.market, A.encode_used_salt(o.buyer, o.salt)),
                (o.currency, O.encode_balance_of(holder)), (o.currency, O.encode_balance_of(act.market)))
    l = act.call.listing
    return ((l.collection, S.encode_owner_of(l.id)),
            (act.market, A.encode_used_salt(l.seller, l.salt)))


def watch_native(act: BuyerAct) -> Tuple[str, ...]:
    """The market's own native balance: it must hold nothing after but what it books owed."""
    return (act.market,)


def _view(deltas, to, data):
    key = (str(to).lower(), str(data).lower())
    return ((getattr(deltas, "views_before", None) or {}).get(key),
            (getattr(deltas, "views_after", None) or {}).get(key))


def _words(log):
    data = str(log.get("data") or "0x").lower().removeprefix("0x")
    return [data[i:i + 64] for i in range(0, len(data) - 63, 64)]


def _addr(word: str) -> str:
    return "0x" + str(word)[-40:].lower()


def assert_simulation(act: BuyerAct, intent, deltas, holder: str) -> Optional[str]:
    """The refusal the simulated buyer call earns beyond the generic checks, or None."""
    import core.wallet.sale_conditions as S
    if act.kind == KIND_ACCEPT:
        return _accept_assert(act, deltas, holder)
    me = str(holder).lower()
    me_t = "0x" + "0" * 24 + me.removeprefix("0x")
    moved = [name for name, rows in (
        ("token transfers", getattr(deltas, "holder_transfers", ())),
        ("token approvals", getattr(deltas, "holder_approvals", ())),
        ("NFT moves out", getattr(deltas, "holder_nft_out", ())),
        ("operator approvals", getattr(deltas, "holder_operator_grants", ())),
        ("NFT approvals", getattr(deltas, "holder_nft_approvals", ())),
        ("Permit2 approvals", getattr(deltas, "holder_permit2_grants", ())),
        ("ERC-6909 approvals", getattr(deltas, "holder_6909_approvals", ()))) if rows]
    moved += [f"token {t} {d:+d}" for t, d in (getattr(deltas, "token_deltas", None) or {}).items() if d]
    if moved:
        return (f"the {act.kind} moves or approves more of the treasury than it declares "
                f"({', '.join(moved)})")
    market_delta = (getattr(deltas, "watched_native_deltas", None) or {}).get(act.market)
    if market_delta is None:
        return f"the simulation did not measure the market's own native balance"
    if act.kind == KIND_CLAIM:
        if getattr(deltas, "holder_nft_in", ()):
            return "the claim brings an NFT in — a claim from the market receives native value only"
        before, after = _view(deltas, act.market, S.encode_owed(me))
        if before is None or after is None:
            return "the simulation did not measure owed(treasury) before and after the claim"
        if after != 0 or before <= 0:
            return f"owed(treasury) goes {before} → {after} — a claim pays it out to 0"
        if int(deltas.native_delta) < before:
            return (f"the treasury receives {deltas.native_delta} wei, less than the {before} "
                    f"the market owed")
        if market_delta != -before:
            return (f"the market's balance moves {market_delta} wei, not the {-before} it paid "
                    f"out — the claim does something else too")
        for log in getattr(deltas, "logs", None) or ():
            if str(log.get("address") or "").lower() == act.market:
                return "the market emits an event in a claim (it emits none)"
            if me_t in [str(t).lower() for t in (log.get("topics") or [])][1:]:
                return f"a log from {log.get('address')} names the treasury in a claim"
        return None
    # buy
    l = act.call.listing
    want_in = [(l.collection, "erc721", l.seller, int(l.id), 1)]
    got_in = [(str(c).lower(), str(s).lower(), str(f).lower(), int(i), int(a))
              for (c, s, f, i, a) in (getattr(deltas, "holder_nft_in", ()) or ())]
    if got_in != want_in:
        return (f"the buy brings in {got_in or 'nothing'}, not exactly #{l.id} of {l.collection} "
                f"from the seller {l.seller}")
    if int(deltas.native_delta) >= 0:
        return "the simulation measures no native payment for the buy"
    sold, owed = [], 0
    for log in getattr(deltas, "logs", None) or ():
        emitter = str(log.get("address") or "").lower()
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        head = topics[0] if topics else ""
        if emitter == act.market:
            words = _words(log)
            if head == S.TOPIC_SOLD and len(topics) == 4 and len(words) == 4:
                sold.append((_addr(topics[1]), int(topics[2], 16), _addr(topics[3]),
                             _addr(words[0]), _addr(words[1]), int(words[2], 16),
                             int(words[3], 16)))
            elif head == S.TOPIC_OWED and len(topics) == 2 and len(words) == 1:
                if _addr(topics[1]) == me:
                    return "the market books ETH owed to the treasury in a buy"
                owed += int(words[0], 16)
            else:
                return (f"the market emits an event that is not part of a buy "
                        f"({head[:10] or 'anonymous'})")
            continue
        if me_t in topics[1:]:
            if not (emitter == l.collection and head == S.TOPIC_TRANSFER and len(topics) == 4
                    and topics[1:] == ["0x" + "0" * 24 + l.seller[2:], me_t,
                                       "0x" + format(int(l.id), "064x")]):
                return (f"{emitter} emits {head[:10] or 'an anonymous event'} naming the treasury "
                        f"— a buy names it only in the NFT's Transfer")
    if len(sold) != 1:
        return f"the simulation shows {len(sold)} Sold events from the market — a buy emits one"
    coll, sid, seller, buyer, currency, price, royalty = sold[0]
    if (coll, sid, seller, buyer, currency) != (l.collection, int(l.id), l.seller, me, S.ZERO):
        return (f"the market's Sold names {coll} #{sid} from {seller} to {buyer} in {currency}, "
                f"not this listing to the treasury in ETH")
    if price != int(l.price) or royalty > int(l.maxRoyalty) or royalty > price:
        return (f"the market's Sold says price {price} royalty {royalty}; the listing says "
                f"{l.price} with at most {l.maxRoyalty}")
    if market_delta != owed:
        return (f"the market's balance moves {market_delta} wei but it books {owed} owed — it "
                f"holds something after the buy")
    owner = _view(deltas, l.collection, S.encode_owner_of(l.id))
    import core.wallet.sale_acts as A
    salt = _view(deltas, act.market, A.encode_used_salt(l.seller, l.salt))
    if None in owner or None in salt:
        return "the simulation did not measure ownerOf and the listing's salt before and after"
    if owner != (int(l.seller, 16), int(me, 16)):
        return (f"ownerOf(#{l.id}) goes {hex(owner[0])} → {hex(owner[1])}, not the seller → the "
                f"treasury")
    if salt != (0, 1):
        return f"the listing's salt goes {salt[0]} → {salt[1]}, not unused → used"
    return None


def _accept_assert(act: BuyerAct, deltas, holder: str) -> Optional[str]:
    """The measured result of an accept (see the module text)."""
    import core.wallet.sale_conditions as S
    import core.wallet.sale_orders as O
    import core.wallet.sale_acts as A
    o, tid = act.call.offer, int(act.call.token_id)
    me = str(holder).lower()
    me_t = A.topic_word(me)
    moved = [name for name, rows in (
        ("token transfers out", getattr(deltas, "holder_transfers", ())),
        ("token approvals", getattr(deltas, "holder_approvals", ())),
        ("NFT arrivals", getattr(deltas, "holder_nft_in", ())),
        ("operator approvals", getattr(deltas, "holder_operator_grants", ())),
        ("Permit2 approvals", getattr(deltas, "holder_permit2_grants", ())),
        ("ERC-6909 approvals", getattr(deltas, "holder_6909_approvals", ()))) if rows]
    moved += [f"NFT approval {c} #{i} to {to}"
              for (c, to, i) in (getattr(deltas, "holder_nft_approvals", ()) or ())
              if int(str(to), 16) != 0]
    moved += [f"token {t} {d:+d}" for t, d in (getattr(deltas, "token_deltas", None) or {}).items() if d]
    if moved:
        return f"the accept moves or approves more of the treasury than it declares ({', '.join(moved)})"
    got_out = [(str(c).lower(), str(s).lower(), str(t).lower(), int(i), int(a))
               for (c, s, t, i, a) in (getattr(deltas, "holder_nft_out", ()) or ())]
    if got_out != [(o.collection, "erc721", o.buyer, tid, 1)]:
        return f"the accept moves {got_out or 'nothing'} out, not exactly #{tid} to the buyer {o.buyer}"
    market_delta = (getattr(deltas, "watched_native_deltas", None) or {}).get(act.market)
    if market_delta != 0:
        return f"the market's own ETH moves {market_delta} wei in an accept (it moves none)"
    sold, paid_in = [], []
    for log in getattr(deltas, "logs", None) or ():
        emitter = str(log.get("address") or "").lower()
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        head = topics[0] if topics else ""
        words = _words(log)
        if emitter == act.market:
            if head == S.TOPIC_SOLD and len(topics) == 4 and len(words) == 4:
                sold.append((_addr(topics[1]), int(topics[2], 16), _addr(topics[3]),
                             _addr(words[0]), _addr(words[1]), int(words[2], 16),
                             int(words[3], 16)))
                continue
            return f"the market emits an event that is not part of an accept ({head[:10] or 'anonymous'})"
        if me_t not in topics[1:]:
            continue
        if emitter == o.collection and head == S.TOPIC_TRANSFER and topics[1:] == [
                me_t, A.topic_word(o.buyer), "0x" + format(tid, "064x")]:
            continue
        if emitter == o.collection and head == A.TOPIC_APPROVAL and topics[1:] == [
                me_t, A.topic_word(S.ZERO), "0x" + format(tid, "064x")]:
            continue
        if (emitter == o.currency and head == S.TOPIC_TRANSFER and len(topics) == 3
                and topics[1:] == [A.topic_word(act.market), me_t] and len(words) == 1):
            paid_in.append(int(words[0], 16))
            continue
        return (f"{emitter} emits {head[:10] or 'an anonymous event'} naming the treasury — an "
                f"accept names it only in the token's Transfer and the payment")
    if len(sold) != 1:
        return f"the simulation shows {len(sold)} Sold events from the market — an accept emits one"
    coll, sid, seller, buyer, currency, price, royalty = sold[0]
    if (coll, sid, seller, buyer, currency) != (o.collection, tid, me, o.buyer, o.currency):
        return (f"the market's Sold names {coll} #{sid} from {seller} to {buyer} in {currency}, "
                f"not this offer from the treasury")
    if price != o.price or royalty > o.maxRoyalty or royalty > price:
        return (f"the market's Sold says price {price} royalty {royalty}; the offer says {o.price} "
                f"with at most {o.maxRoyalty}")
    if paid_in != [price - royalty]:
        return f"the treasury is paid {paid_in} by the market, not exactly price − royalty {price - royalty}"
    views = {k: _view(deltas, *k) for k in (
        (o.collection, S.encode_owner_of(tid)), (o.collection, O.encode_get_approved(tid)),
        (act.market, A.encode_used_salt(o.buyer, o.salt)),
        (o.currency, O.encode_balance_of(me)), (o.currency, O.encode_balance_of(act.market)))}
    if any(None in v for v in views.values()):
        return "the simulation did not measure ownerOf, getApproved, the salt and the balances"
    owner, approved, salt, mine, held = views.values()
    if owner != (int(me, 16), int(o.buyer, 16)):
        return f"ownerOf(#{tid}) goes {hex(owner[0])} → {hex(owner[1])}, not the treasury → the buyer"
    if approved[1] != 0:
        return f"getApproved(#{tid}) is {hex(approved[1])} after the accept — the approval survived"
    if salt != (0, 1):
        return f"the offer's salt goes {salt[0]} → {salt[1]}, not unused → used"
    if mine[1] - mine[0] != price - royalty:
        return (f"the treasury's {o.currency} goes {mine[0]} → {mine[1]}, not up by exactly "
                f"price − royalty {price - royalty}")
    if held[0] != held[1]:
        return f"the market's {o.currency} goes {held[0]} → {held[1]} — it holds something after"
    return None


__all__ = ["ACCEPT_VERB", "BUYER_SELECTORS", "BUYER_VERBS", "BuyerAct", "KIND_BUY", "KIND_CLAIM",
           "KIND_ACCEPT", "assert_simulation", "gate", "runtime_refusal", "views", "watch_native"]
