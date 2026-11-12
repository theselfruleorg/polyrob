"""tx_guard rule 5: the pre-flight reads of a token-bound account call (``TxIntent.via_account``).

Moved out of ``core/wallet/tx_guard.py`` (its size row is frozen); ``tx_guard`` keeps the two
names (``_via_account_preflight``, ``_account_collection_refusal``) as thin wrappers, because
the guard steps and the tests reach them there. Helpers of ``tx_guard`` are reached as
``_tg.<name>`` at call time for the same reason (a test monkeypatches them there).

The account kind is the profile's (``collection_registry.AccountVersion.kind``, a row of
``core/wallet/account_kinds.py``); every read that differs by kind reads a field of the row:

* ``kind.has_lock`` — read ``isLocked`` (Tokenbound AccountV3);
* ``kind.has_forwarder`` — read ``isTrustedForwarder`` for EVERY distinct inner target (each leg
  of a batch, not only the spend leg);
* ``kind.journal_getter`` — the account's JournalLog must be the profile's ``journal_log`` when
  the profile pins one (ERC-6551P);
* ``kind.sale_ready_view`` — the sale hold: an automatic act through a READY account refuses
  (115 W7). The seller's sale acts (ready, unready, cancel a market order) are the second half
  of this module.

A kind without a lock or a forwarder (ERC-6551P: no such function, no fallback) would revert
those reads and fail closed on every call, so they are skipped for it.

Every read that fails refuses (fail closed). ⚠️ The signer re-runs this code in its own process:
no new module-level imports.
"""
from typing import Optional, Tuple


def account_collection_refusal(chain: str, account: str, rpc, *,
                               journal_to: Optional[str] = None,
                               implementation: Optional[str] = None
                               ) -> Tuple[Optional[str], object]:
    """C4: the account's own ``token()`` must name a PINNED collection on this chain whose live
    code still hashes to its pin, and the account must be that token's canonical account (one
    of the profile's account versions, pure CREATE2). Returns ``(reason or None, (profile,
    version))``; *version* is the profile's version of *implementation* (None when not given or
    not pinned). The signer re-runs this guard, so a clone of an unpinned or changed collection
    refuses there too. Fail closed."""
    from core.wallet import collection_registry, erc6551
    from core.wallet import tx_guard as _tg
    chain_id, collection, token_id = erc6551.read_token(rpc, account)
    want = collection_registry.chain_id_of(chain)
    if int(chain_id) != int(want):
        return (f"the token-bound account {account} is bound to a token on chain {chain_id}, "
                f"not {chain} ({want})"), (None, None)
    why = _tg._collection_runtime_refusal(chain, collection, rpc)
    if why:
        return f"the token-bound account {account} belongs to {collection} #{token_id}: {why}", \
            (None, None)
    profile = collection_registry.profile_for(want, collection)
    canonical = {erc6551.account_address(want, profile.address, int(token_id), salt=v.salt,
                                         implementation=v.implementation).lower()
                 for v in profile.accounts}
    if str(account).lower() not in canonical:
        return (f"{account} is not the pinned ERC-6551 account of {collection} #{token_id} — "
                f"an account outside the profile's account versions"), (profile, None)
    if journal_to is not None and str(journal_to).lower() != str(profile.journal_log or ""):
        return (f"the journal leg calls {journal_to}, not the JournalLog pinned for {collection} "
                f"({profile.journal_log or 'none'})"), (profile, None)
    version = profile.account_version(implementation) if implementation else None
    return None, (profile, version)


def inner_targets(outer_tx) -> Tuple[str, ...]:
    """Every distinct target the signed account call reaches: the ``execute`` target, or each
    leg of an ``executeBatch`` (lowercase, in order). Empty when the calldata is neither."""
    from core.wallet import erc6551
    data = str((outer_tx or {}).get("data") or "")
    try:
        if data[:10].lower() == erc6551.EXECUTE_BATCH_SELECTOR:
            legs = [to for (to, _v, _d, _op) in erc6551.decode_execute_batch(data)]
        else:
            legs = [erc6551.decode_execute(data)[0]]
    except Exception:  # noqa: BLE001 — not an account call: the guard refuses it elsewhere
        return ()
    out = []
    for to in legs:
        if str(to).lower() not in out:
            out.append(str(to).lower())
    return tuple(out)


def via_account_preflight(intent, sender: str, rpc, *, inner_to=None,
                          journal_to: Optional[str] = None, owner_asked: bool = False,
                          sale_act=None, notes: Optional[list] = None) -> Optional[str]:
    """Rule 5: reads BEFORE simulation. Any failed read refuses (fail closed).

    069 v4 (the simple model): the SIGNER must be the NFT's current owner — the only way this
    instance acts through an account. There is no grant path: ``setPermissions`` is an
    account-admin selector the guard refuses, and a permissioned caller is never accepted.

    *inner_to* is one target or a tuple of every inner target (:func:`inner_targets`).

    The SALE HOLD (115 W7, finding B4; 111 §8.4): for a kind with ``sale_ready_view`` one more
    read — the collection's readiness of the account's own token. Any act through the account
    ends readiness (the account changes ``state()`` at the start of every batch), so while it is
    ready an AUTOMATIC act refuses (:data:`sale_acts.HOLD_TEXT`); an act the owner asked for
    (*owner_asked*: step 0's ``owner_direct`` or ``owner_granted``) passes and *notes* gets the
    sentence that readiness ends. An unreadable readiness refuses an automatic act (it is never
    read as "not ready"). The ready act itself (*sale_act* ``ready``) is not held: re-making it
    ready is what it does, and its journal leg rides the same batch. A missing context (the
    signer's re-check) is never the owner, so there the hold refuses: unready first."""
    from core.wallet import erc6551
    acct = intent.via_account
    try:
        impl = erc6551.read_implementation(rpc, acct)
        why, (profile, version) = account_collection_refusal(
            intent.chain, acct, rpc, journal_to=journal_to, implementation=impl)
        if why:
            return why
        if version is None:
            return (f"the token-bound account clones {impl}, not an account implementation "
                    f"pinned for {profile.address} (one of its account versions: "
                    f"{', '.join(v.kind.label for v in profile.accounts)})")
        erc6551.verify(rpc, version)
        kind = version.kind
        if kind.has_lock and erc6551.read_is_locked(rpc, acct):
            return "the token-bound account is LOCKED by its owner — nothing moves until the lock ends"
        owner = erc6551.read_owner(rpc, acct)
        if str(owner).lower() != str(sender).lower():
            return (f"this wallet ({sender}) does not own the NFT of the token-bound account "
                    f"{acct} (its owner is {owner}) — an agent acts through an account only "
                    f"as the NFT's current owner")
        targets = (inner_to,) if isinstance(inner_to, str) else tuple(inner_to or ())
        for target in targets if kind.has_forwarder else ():
            if target and erc6551.read_is_trusted_forwarder(rpc, acct, target):
                return (f"the token-bound account trusts {target} as an ERC-2771 forwarder — "
                        f"the inner call may not go through it")
        if kind.journal_getter and profile.journal_log:
            journal = erc6551.read_journal(rpc, acct)
            if journal != profile.journal_log:
                return (f"the {kind.label} {acct} names the JournalLog {journal}, not the one "
                        f"pinned for {profile.address} ({profile.journal_log})")
        hold = _sale_hold(rpc, profile, kind, acct, owner_asked=owner_asked,
                          sale_act=sale_act, notes=notes)
        if hold:
            return hold
        state = erc6551.read_state(rpc, acct)
        if state != int(intent.via_account_state):
            return (f"the token-bound account state moved ({intent.via_account_state} → "
                    f"{state}) since the intent was built — re-read and rebuild")
    except Exception as exc:  # noqa: BLE001
        return f"Account pre-flight could not be verified ({exc}); failing closed"
    return None


def _sale_hold(rpc, profile, kind, account: str, *, owner_asked: bool, sale_act,
               notes: Optional[list]) -> Optional[str]:
    """The sale hold of :func:`via_account_preflight` (None = the act may go on)."""
    from core.wallet import erc6551, sale_acts
    if not kind.sale_ready_view or (sale_act and sale_act[0] == sale_acts.KIND_READY):
        return None
    try:
        _chain_id, collection, token_id = erc6551.read_token(rpc, account)
        ready = sale_acts.read_sale_ready(rpc, collection, token_id)
    except Exception as exc:  # noqa: BLE001 — unreadable is never "not ready"
        if not owner_asked:
            return (f"the sale readiness of the {kind.label} {account} ({kind.sale_ready_view} "
                    f"on {profile.address}) could not be read ({exc}) — a ready account is on "
                    f"hold, so an automatic act through it refuses; failing closed")
        if notes is not None:
            notes.append(sale_acts.HOLD_UNREAD_NOTE)
        return None
    if not ready:
        return None
    if not owner_asked:
        return sale_acts.HOLD_TEXT
    if notes is not None:
        notes.append(sale_acts.HOLD_NOTE)
    return None


# ---------------------------------------------------------------------------------------------
# The seller's sale acts (115 W7; 111 §8.1 ready, cancel_ready, cancel_listing). The calls, the
# events and the reads are core/wallet/sale_acts.py; here the guard's half: the shape (no RPC),
# the RPC pins, the views the simulation reads before and after, and the measured result. Each
# act is declared as ``is_nft_op`` with NOTHING to move, and recognised by its PINNED target and
# exact calldata — so no other intent can make these calls and no act can move anything.

def _sale_rails(chain: str):
    """``({collection: profile}, {market: profile})``: the pinned collections whose account
    kinds have a sale-ready view, and the pinned markets (``standard.erc6551p.market``)."""
    from core.wallet import collection_registry as registry
    profiles = registry.profiles_on(registry.chain_id_of(chain))
    collections = {p.address: p for p in profiles
                   if any(v.kind.sale_ready_view for v in p.accounts)}
    markets = {p.erc6551p.market: p for p in profiles if p.erc6551p is not None}
    return collections, markets


def sale_act_gate(intent, tx) -> Tuple[Optional[tuple], Optional[str]]:
    """``((kind, arg, target), None)`` for a well-formed sale act, ``(None, None)`` for an intent
    that makes none, ``(None, why)`` to refuse. *tx* is the INNER call under ``via_account``.
    No RPC; an untrusted registry pins nothing here (the pre-flight refuses an account act)."""
    from core.wallet import sale_acts as A
    try:
        collections, markets = _sale_rails(intent.chain)
    except Exception:  # noqa: BLE001
        return None, None
    to = str((tx or {}).get("to") or "").lower()
    data = str((tx or {}).get("data") or "").lower()
    if to in collections:
        kind, arg, why = A.collection_act(data)
        if kind is None and why is None:
            return None, None
        where = f"the pinned collection {to}"
    elif to in markets:
        from core.wallet.tx_guard_sale import BUYER_SELECTORS
        if data[:10] in BUYER_SELECTORS:
            return None, None   # 115 W9: the buyer's calls — tx_guard_sale.gate, and only it
        kind, arg, why = A.market_act(data)
        where = f"the pinned market {to}"
    else:
        return None, None
    if why:
        return None, f"{where}: {why}"
    if kind == A.KIND_APPROVE:
        want = collections[to].erc6551p.market if collections[to].erc6551p is not None else None
        if not want or str(arg[0]).lower() != want:
            return None, (f"approve({arg[0]}, #{arg[1]}) on {where}: a single-token approval of a "
                          f"pinned collection's token goes only to its PINNED market "
                          f"({want or 'none pinned'}), for a listing or an accept")
    other = [name for name, on in (
        ("a deployment", intent.is_deploy), ("a claim", intent.is_claim),
        ("a registration", intent.is_registration),
        ("an allowance operation", intent.is_allowance_op),
        ("a liquidity operation", intent.is_liquidity_op),
        ("a collection reveal", intent.is_collection_reveal),
        ("a collection mint", intent.is_collection_mint),
        ("a resource call", intent.is_resource_call), ("a journal entry", intent.is_journal_entry),
        ("an approve-spend-reset batch", intent.via_account_batch),
        ("a transfer", intent.is_transfer)) if on]
    declared = [name for name, v in (
        ("token", intent.token), ("amount_raw", intent.amount_raw),
        ("inflow_token", intent.inflow_token), ("min_inflow_raw", intent.min_inflow_raw),
        ("min_native_inflow_wei", intent.min_native_inflow_wei),
        ("allowance grants", intent.expected_allowance_grants),
        ("nft_out", intent.nft_out), ("expected_nft_in", intent.expected_nft_in),
        ("nft_operator_ops", intent.nft_operator_ops),
        ("nft_approval_revokes", intent.nft_approval_revokes),
        ("erc6909_revokes", intent.erc6909_revokes), ("permit2_revokes", intent.permit2_revokes),
        ("mint_ids", intent.mint_ids), ("reveal_ids", intent.reveal_ids),
        ("expected_events", intent.expected_events)) if v]
    if not intent.is_nft_op or other or declared:
        what = ", ".join(other + declared) or "not an NFT operation"
        return None, (f"{data[:10]} on {where} is a sale act ({kind}): only the guarded "
                      f"{A.SALE_ACT_VERBS} make it, declared as an NFT operation that moves "
                      f"nothing (this intent: {what})")
    if str(intent.to or "").lower() != to:
        return None, (f"the sale act calls {to} but the intent is addressed to {intent.to} — the "
                      f"authorized target and the called one must be the same contract")
    try:
        value = int((tx or {}).get("value") or 0)
    except (TypeError, ValueError):
        return None, "the sale act's value is unreadable"
    if value != 0:
        return None, f"a sale act carries no value; this call sends {value} wei"
    if kind == A.KIND_READY and not intent.via_account:
        return None, ("readyToSell is made by the token's OWN account (the collection refuses any "
                      "other caller, NotTokenAccount): declare `via_account`")
    if kind != A.KIND_READY and intent.via_account:
        return None, (f"the {kind} act is sent by the treasury itself, not through the account "
                      f"(an account batch would end the sale readiness on its own)")
    return (kind, arg, to), None


def sale_act_runtime_refusal(act, intent, tx, holder: str, sender: Optional[str], rpc
                             ) -> Optional[str]:
    """The RPC pins of a sale act: the collection's code (and the market's), and for ``ready``
    the id is the account's OWN token and the treasury is no ERC-6551 account; for ``unready``
    the treasury owns the token. Fail closed."""
    from core.wallet import collection_registry as registry
    from core.wallet import erc6551, sale_acts as A
    from core.wallet.nft_account import read_owner_of
    kind, arg, target = act
    try:
        collections, markets = _sale_rails(intent.chain)
        if kind == A.KIND_APPROVE:
            profile = collections[target]
            std = profile.erc6551p
            why = (registry.runtime_refusal(rpc, profile)
                   or registry.code_pin_refusal(rpc, std.market, std.market_runtime_sha256,
                                                "pinned market"))
            if why:
                return why
            owner = read_owner_of(rpc, target, int(arg[1]))
            if str(owner).lower() != str(holder).lower():
                return (f"approve(market, #{arg[1]}): the NFT's owner is {owner}, not this "
                        f"treasury {holder}")
            _account, version = profile.account_of(int(arg[1]))
            if not version.kind.sale_ready_view:
                return f"the account kind of {target} #{arg[1]} has no sale rule to list under"
            return None
        if kind in (A.KIND_READY, A.KIND_UNREADY):
            profile = collections[target]
            why = registry.runtime_refusal(rpc, profile)
            if why:
                return why
        else:
            profile = markets[target]
            std = profile.erc6551p
            return (registry.runtime_refusal(rpc, profile)
                    or registry.code_pin_refusal(rpc, std.market, std.market_runtime_sha256,
                                                 "pinned market"))
        if kind == A.KIND_READY:
            account = str(intent.via_account)
            _cid, collection, token_id = erc6551.read_token(rpc, account)
            if str(collection).lower() != target or int(token_id) != int(arg):
                return (f"readyToSell names #{arg} of {target}, but the account {account} is the "
                        f"account of {collection} #{token_id} — an account makes ready only its "
                        f"OWN token")
            version = profile.version_for_account(int(token_id), account)
            if version is None or not version.kind.sale_ready_view:
                return (f"the account {account} is no account kind of {target} with a sale-ready "
                        f"view — nothing here can make it ready")
            if A.is_erc6551_clone(rpc("eth_getCode", [sender, "latest"])):
                return (f"the treasury {sender} is itself an ERC-6551 account — the collection "
                        f"refuses an account as the seller (SellerIsAccount): its control can "
                        f"change without a transfer of this token")
            return None
        owner = read_owner_of(rpc, target, int(arg))
        if str(owner).lower() != str(holder).lower():
            return (f"cancelReady(#{arg}): the NFT's owner is {owner}, not this treasury "
                    f"{holder} — only the owner ends its readiness")
        _account, version = profile.account_of(int(arg))
        if not version.kind.sale_ready_view:
            return f"the account kind of {target} #{arg} has no sale readiness to end"
    except Exception as exc:  # noqa: BLE001
        return f"the sale act's pins could not be read ({exc}); failing closed"
    return None


def sale_act_views(act, holder: str) -> Tuple[Tuple[str, str], ...]:
    """The ``(to, data)`` views the simulation reads before and after a sale act (*holder* is the
    signing treasury for a market act)."""
    from core.wallet import sale_acts as A
    kind, arg, target = act
    if kind == A.KIND_APPROVE:
        return ((target, A.encode_get_approved(int(arg[1]))),)
    if kind in (A.KIND_READY, A.KIND_UNREADY):
        return ((target, A.encode_sale_ready_of(int(arg))),)
    if kind == A.KIND_CANCEL_SALT:
        return ((target, A.encode_used_salt(holder, int(arg))),)
    return ((target, A.encode_nonces(holder)),)


def sale_act_treasury_deltas(act, deltas, sender: Optional[str]):
    """*deltas* without the one event a ready act names the treasury in — ``SaleReady(id,
    owner = the treasury, state)`` from the collection — for the "nothing on the treasury side"
    rule of an account call. Every other log is kept."""
    import dataclasses
    from core.wallet import sale_acts as A, simulation
    kind, _arg, target = act
    if kind != A.KIND_READY or not sender:
        return deltas
    logs = tuple(lg for lg in (deltas.logs or ()) if not (
        str(lg.get("address") or "").lower() == target
        and [str(t).lower() for t in (lg.get("topics") or [])][:1] == [A.TOPIC_SALE_READY]))
    return dataclasses.replace(deltas, logs=logs,
                               sender_moved=simulation._sender_movements(list(logs), sender))


def sale_act_assert(act, intent, deltas, holder: str, sender: Optional[str],
                    journal_to: Optional[str]) -> Optional[str]:
    """The refusal the simulated sale act earns, or None: nothing of the holder moves or is
    approved, every log comes from the act's contract (and the pinned JournalLog for a
    journaled ready), its event is emitted for the declared id or salt, and the views read
    after the act show its effect."""
    from core.wallet import sale_acts as A
    kind, arg, target = act
    treasury = str(sender or holder).lower()
    if kind == A.KIND_APPROVE:
        return _approve_assert(act, deltas, treasury)
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
        return (f"the {kind} act moves or approves something ({', '.join(moved)}) — a sale act "
                f"moves nothing")
    me, acct = A.topic_word(treasury), A.topic_word(holder)
    emitters = {target} | ({str(journal_to).lower()} if journal_to else set())
    seen = []
    for log in getattr(deltas, "logs", None) or ():
        emitter = str(log.get("address") or "").lower()
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if emitter not in emitters:
            return (f"the {kind} act makes {emitter} emit an event — every log of it comes from "
                    f"{target}" + (f" or the JournalLog {journal_to}" if journal_to else ""))
        if journal_to and emitter == str(journal_to).lower():
            continue   # the journal leg: checked as an entry of this account (step 2b)
        head = topics[0] if topics else ""
        if kind == A.KIND_READY and head == A.TOPIC_SALE_READY:
            if len(topics) != 3 or int(topics[1], 16) != int(arg) or topics[2] != me:
                return (f"the collection emits SaleReady for another id or owner "
                        f"({topics[1:3]}), not #{arg} of the treasury {treasury}")
            seen.append(head)
        elif kind == A.KIND_CANCEL_SALT and head == A.TOPIC_SALT_CANCELLED:
            word = str(log.get("data") or "0x").lower().removeprefix("0x")
            if len(topics) != 2 or topics[1] != me or len(word) != 64 or int(word, 16) != int(arg):
                return f"the market cancels another signer's or another salt ({topics[1:]}, 0x{word})"
            seen.append(head)
        elif kind == A.KIND_CANCEL_ALL and head == A.TOPIC_NONCE_INCREMENTED:
            word = str(log.get("data") or "0x").lower().removeprefix("0x")
            if len(topics) != 2 or topics[1] != me or len(word) != 64:
                return f"the market increments another signer's nonce ({topics[1:]})"
            seen.append(int(word, 16))
        elif kind in (A.KIND_CANCEL_SALT, A.KIND_CANCEL_ALL) or head == A.TOPIC_SALE_READY:
            return f"the {kind} act emits an event that is not part of it ({head[:10] or 'anonymous'})"
        elif me in topics[1:] or acct in topics[1:]:
            return (f"the collection emits {head[:10] or 'an anonymous event'} naming the "
                    f"treasury or the account — not part of the {kind} act")
    view = next(iter(sale_act_views(act, treasury)))
    key = (view[0].lower(), view[1].lower())
    before = (getattr(deltas, "views_before", None) or {}).get(key)
    after = (getattr(deltas, "views_after", None) or {}).get(key)
    if before is None or after is None:
        return f"the simulation did not measure the {kind} act's effect (a view read before and after)"
    if kind == A.KIND_READY:
        if len(seen) != 1:
            return f"the simulation shows {len(seen)} SaleReady events — a ready act emits exactly one"
        if after != 1:
            return f"saleReadyOf(#{arg}) is {after} after the act — it does not make the NFT ready"
    elif kind == A.KIND_UNREADY:
        if after != 0:
            return f"saleReadyOf(#{arg}) is still {after} after cancelReady — readiness did not end"
    elif kind == A.KIND_CANCEL_SALT:
        if len(seen) != 1 or after != 1:
            return (f"the market shows {len(seen)} SaltCancelled event(s) and usedSalt {after} — "
                    f"a cancel marks exactly this salt used")
    else:
        if seen != [before + 1] or after != before + 1:
            return (f"the market's nonce goes {before} → {after} with NonceIncremented {seen} — "
                    f"incrementNonce raises it by exactly one")
    return None


def _approve_assert(act, deltas, treasury: str) -> Optional[str]:
    """``approve(market, id)``: exactly ONE ``Approval(treasury, market, id)`` from the
    collection, nothing else moves or is approved, and ``getApproved(id)`` is the market after."""
    from core.wallet import sale_acts as A
    _kind, (market, token_id), target = act
    moved = [name for name, rows in (
        ("token transfers", getattr(deltas, "holder_transfers", ())),
        ("token approvals", getattr(deltas, "holder_approvals", ())),
        ("NFT moves out", getattr(deltas, "holder_nft_out", ())),
        ("NFT arrivals", getattr(deltas, "holder_nft_in", ())),
        ("operator approvals", getattr(deltas, "holder_operator_grants", ())),
        ("Permit2 approvals", getattr(deltas, "holder_permit2_grants", ())),
        ("ERC-6909 approvals", getattr(deltas, "holder_6909_approvals", ()))) if rows]
    moved += [f"token {t} {d:+d}" for t, d in (getattr(deltas, "token_deltas", None) or {}).items() if d]
    if moved:
        return f"the market approval moves or approves something else ({', '.join(moved)})"
    approvals = [(str(c).lower(), str(o).lower(), int(i))
                 for (c, o, i) in (getattr(deltas, "holder_nft_approvals", ()) or ())]
    if approvals != [(target, str(market).lower(), int(token_id))]:
        return (f"the simulation shows the NFT approvals {approvals}, not exactly #{token_id} of "
                f"{target} to its pinned market {market}")
    want = [A.TOPIC_APPROVAL, A.topic_word(treasury), A.topic_word(market),
            "0x" + format(int(token_id), "064x")]
    seen = 0
    for log in getattr(deltas, "logs", None) or ():
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if str(log.get("address") or "").lower() != target or topics != want:
            return (f"the market approval emits {topics[:1] or 'an anonymous event'} from "
                    f"{log.get('address')} — it emits one Approval(treasury, market, id) only")
        seen += 1
    view = (target, A.encode_get_approved(int(token_id)))
    after = (getattr(deltas, "views_after", None) or {}).get(view)
    if seen != 1 or after is None or after != int(market, 16):
        return (f"the market approval shows {seen} Approval event(s) and getApproved(#{token_id}) "
                f"{hex(after) if after is not None else 'unmeasured'} after — not the market")
    return None


__all__ = ["account_collection_refusal", "inner_targets", "sale_act_assert", "sale_act_gate",
           "sale_act_runtime_refusal", "sale_act_treasury_deltas", "sale_act_views",
           "via_account_preflight"]
