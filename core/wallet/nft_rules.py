"""The ERC-6551 rules for every collection (124 W1, W2) — read from the chain, never from a pin.

A pin is the owner's AUTONOMY grant (proposal 124 section 5); it never decides whether a token
may brick itself. So each rule here derives what it needs from the chain:

* the account implementations of a collection — every reviewed kind's built-in implementation
  (``core.wallet.account_kinds``), the collection's own ``accountImplementation()`` when it
  names one, and a pinned profile's versions when the registry can be read (a pin only ADDS an
  implementation to derive; an unreadable registry removes no rule);
* the ids a transaction can touch — the moved token ids and every small ABI word of the
  calldata (124 F2: the derivation runs for the ids in the transaction, not ``0..max_supply``),
  plus a pinned collection's whole offline set when one is pinned;
* the owner of an account — ``ownerOf`` of the token in the clone's footer, walked up to
  :data:`MAX_WALK` levels.

"Unreadable is not empty" (124 section 10.2 rule 5): a fact this module cannot read refuses the
act it guards. A fact about a collection the transaction does not touch is never read.

Called from the guard's measure steps (``core/wallet/tx_guard_steps/measure.py``) through the
thin names in ``core/wallet/tx_guard.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

Rpc = Callable[[str, list], object]

_ZERO = "0x" + "0" * 40
_DEAD = "0x000000000000000000000000000000000000dead"
#: 124 N6: the owner chain of a receiving account is walked at most this deep.
MAX_WALK = 4
#: An ABI word below this is read as a token id the transaction may touch (124 F2).
_ID_WORD_LIMIT = 1 << 64
_ACCOUNT_IMPLEMENTATION = "0x11464fbe"  # accountImplementation()
_OWNER_OF = "0x6352211e"                # ownerOf(uint256)


class RuleReadError(RuntimeError):
    """A chain fact a rule needs could not be read."""


# ---------------------------------------------------------------------------- the reads

def _word_addr(word: str) -> str:
    return "0x" + word[24:]


def calldata_words(tx: Optional[dict]) -> List[str]:
    """The 32-byte ABI words of the calldata after the selector (lowercase hex, no 0x)."""
    data = str((tx or {}).get("data") or "").lower().removeprefix("0x")
    body = data[8:] if len(data) >= 8 else ""
    return [body[i:i + 64] for i in range(0, len(body) - 63, 64)]


def calldata_addresses(tx: Optional[dict]) -> Set[str]:
    """Every ABI word of the calldata that is shaped like an address (12 zero bytes, then 20)."""
    return {_word_addr(w) for w in calldata_words(tx)
            if w[:24] == "0" * 24 and int(w[24:], 16) >= _ID_WORD_LIMIT}


#: The most calldata words read as candidate ids (the derivation is pure keccak per word).
_MAX_ID_WORDS = 64


def calldata_ids(tx: Optional[dict]) -> Set[int]:
    """Every ABI word of the calldata as a candidate token id (124 F2) — large or hashed ids
    included; at most :data:`_MAX_ID_WORDS` words."""
    return {int(w, 16) for w in calldata_words(tx)[:_MAX_ID_WORDS]}


def _eth_call(rpc: Rpc, to: str, data: str):
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def account_implementation(rpc: Rpc, collection: str) -> Optional[str]:
    """The collection's own ``accountImplementation()``, or None when it names none (a revert,
    empty data, or the zero address). An RPC failure RAISES :class:`RuleReadError`."""
    try:
        raw = _eth_call(rpc, collection, _ACCOUNT_IMPLEMENTATION)
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        if "revert" in text or "execution reverted" in text:
            return None
        raise RuleReadError(f"accountImplementation() of {collection} could not be read ({exc})")
    raw = str(raw or "").lower().removeprefix("0x")
    if len(raw) < 64:
        return None
    addr = "0x" + raw[24:64]
    return None if int(addr, 16) == 0 else addr


def owner_of(rpc: Rpc, collection: str, token_id: int) -> Optional[str]:
    """``ownerOf(token_id)`` lowercase, or None for a revert or the zero address (not minted or
    burned). An RPC failure RAISES :class:`RuleReadError`."""
    try:
        raw = _eth_call(rpc, collection, _OWNER_OF + format(int(token_id), "064x"))
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        if "revert" in text:
            return None
        raise RuleReadError(f"ownerOf({token_id}) on {collection} failed: {exc}")
    raw = str(raw or "").lower().removeprefix("0x")
    if len(raw) < 64:
        return None
    addr = "0x" + raw[24:64]
    return None if int(addr, 16) == 0 else addr


def _pinned_profiles(chain_id: int) -> list:
    """The pinned profiles on *chain_id*; an unreadable registry is no pin (124 section 5.5)."""
    try:
        from core.wallet import collection_registry
        return list(collection_registry.profiles_on(chain_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("nft_rules: the collection registry is unreadable (%s); no pin applies", exc)
        return []


def implementations(rpc: Rpc, chain_id: int, collection: str) -> List[Tuple[str, int]]:
    """Every ``(implementation, salt)`` whose account of a *collection* token this build can
    derive: each reviewed kind's built-in implementation, the collection's own
    ``accountImplementation()``, and a pinned profile's versions. Raises
    :class:`RuleReadError` when the collection's own read fails."""
    from core.wallet import account_kinds
    out: Dict[str, int] = {}
    for kind in account_kinds.KINDS.values():
        if kind.builtin_implementation:
            out[kind.builtin_implementation.lower()] = 0
    c = str(collection).lower()
    pinned = [p for p in _pinned_profiles(chain_id) if p.address == c]
    for p in pinned:
        for v in p.accounts:
            out[str(v.implementation).lower()] = int(v.salt)
    if not pinned:   # a pin already holds the implementation it proved at trust time
        own = account_implementation(rpc, collection)
        if own:
            out.setdefault(own.lower(), 0)
    return sorted(out.items())


def account_addresses(chain_id: int, collection: str, ids: Iterable[int],
                      impls: Sequence[Tuple[str, int]]) -> Dict[str, int]:
    """``{account address (lowercase): token id}`` for *ids* under every implementation."""
    from core.wallet import erc6551
    out: Dict[str, int] = {}
    for k in ids:
        for impl, salt in impls:
            out[erc6551.account_address(chain_id, collection, int(k), salt=salt,
                                        implementation=impl).lower()] = int(k)
    return out


def _has_code(rpc: Rpc, address: str) -> bool:
    try:
        code = rpc("eth_getCode", [address, "latest"])
    except Exception as exc:  # noqa: BLE001
        raise RuleReadError(f"the code at {address} could not be read ({exc})")
    return isinstance(code, str) and len(code) > 2


# ---------------------------------------------------------------------------- the agent's accounts

@dataclass(frozen=True)
class HeldAccount:
    chain: str
    collection: str
    token_id: int
    account: str
    label: str


def held_accounts() -> Optional[List[HeldAccount]]:
    """The accounts of the NFTs the treasury holds, on every chain (the holdings watch's state),
    or None when that state cannot be read."""
    try:
        from core.wallet import holdings_state
        rows = holdings_state.tracked()
    except Exception as exc:  # noqa: BLE001
        logger.warning("nft_rules: the holdings state is unreadable (%s)", exc)
        return None
    out = []
    for r in rows:
        account = str(r.get("account") or "").lower()
        if account:
            out.append(HeldAccount(chain=str(r.get("chain") or ""),
                                   collection=str(r.get("collection") or "").lower(),
                                   token_id=int(r.get("token_id") or 0), account=account,
                                   label=str(r.get("label") or "")))
    return out


def transaction_addresses(intent, tx: Optional[dict], deltas=None) -> Set[str]:
    """Every address the transaction names: the declared and the called destination, each
    measured transfer's counterparty, each measured NFT move, and every address-shaped ABI word."""
    out = {str(getattr(intent, "to", "") or ""), str((tx or {}).get("to") or "")}
    if deltas is not None:
        out.update(str(d) for (_t, d, _a) in getattr(deltas, "holder_transfers", ()) or ())
        out.update(str(d) for (_c, _s, d, _i, _a) in getattr(deltas, "holder_nft_out", ()) or ())
    out |= calldata_addresses(tx)
    return {a.lower() for a in out if a and a.lower() not in (_ZERO, _DEAD)}


def cross_chain_refusal(chain: str, addresses: Iterable[str],
                        held: Optional[List[HeldAccount]] = None) -> Optional[str]:
    """124 C1: an address that is the ERC-6551 account of an NFT the agent holds on ANOTHER chain
    has the same address here, and here nobody controls it (``owner()`` is the zero address on
    an ERC-6551P account; Tokenbound's trusted executors on v3) — whatever goes there is lost.
    An unreadable holdings state checks nothing (it is logged): it never blocks a send."""
    held = held_accounts() if held is None else held
    if not held:
        return None
    names = {a.lower() for a in addresses}
    for h in held:
        if h.account in names and h.chain and h.chain != chain:
            return (f"{h.account} is the account of {h.label or h.collection} #{h.token_id} on "
                    f"{h.chain}; on {chain} nobody controls that address, so what goes there is "
                    f"lost. Send on {h.chain}, or to the treasury")
    return None


# ---------------------------------------------------------------------------- nesting (N4-N7)

@dataclass(frozen=True)
class Nesting:
    """What the nesting rules found for one transaction."""
    refusal: Optional[str] = None
    #: 124 N4: ``(moved token text, account)`` for each NFT that goes into an account the agent
    #: itself holds — allowed only with the owner's card.
    nested: Tuple[Tuple[str, str], ...] = ()

    def card_line(self) -> str:
        return "; ".join(
            f"{what} will be nested in {acct} (an account the agent holds); to use it again, "
            f"sweep it to the treasury first" for what, acct in self.nested)


def _walk_to_holder(rpc: Rpc, chain_id: int, start: Tuple[str, int], moved: Tuple[str, int],
                    holder: str) -> Tuple[Optional[str], bool]:
    """Walk the owner chain of the token *start* (the token whose account receives *moved*).
    ``(refusal, ends_at_holder)``: a refusal names a cycle, an unreadable owner or a chain deeper
    than :data:`MAX_WALK`."""
    from core.wallet import erc6551
    cur = (start[0].lower(), int(start[1]))
    for _level in range(MAX_WALK):
        if cur == (moved[0].lower(), int(moved[1])):
            return (f"the receiving account belongs to a chain of accounts owned by "
                    f"{moved[0]} #{moved[1]} itself — a cycle locks both NFTs for good"), False
        try:
            owner = owner_of(rpc, cur[0], cur[1])
        except RuleReadError as exc:
            return f"{exc} — the owner chain of the receiving account is unknown; failing closed", False
        if owner is None:
            return (f"{cur[0]} #{cur[1]}, whose account would receive the NFT, has no owner "
                    f"(not minted or burned) — whoever mints it later takes what sits there"), False
        if owner == str(holder).lower():
            return None, True
        try:
            clone = erc6551.read_clone(rpc, owner)
        except Exception as exc:  # noqa: BLE001
            return (f"could not read the code at {owner}, an owner in the chain of the receiving "
                    f"account ({exc}); failing closed"), False
        if clone is None or int(clone[2]) != int(chain_id):
            return None, False
        cur = (str(clone[3]).lower(), int(clone[4]))
    return (f"the receiving account's owner chain is deeper than {MAX_WALK} levels — I do not "
            f"send an NFT where I cannot prove it is no cycle"), False


def nesting(rpc: Rpc, chain: str, chain_id: int, tx: Optional[dict], nft_out,
            holder: str) -> Nesting:
    """124 N4-N7 and C3 for every measured NFT move out of the holder, any collection.

    * N5/N7: the destination is an account of the moved token's OWN collection — deployed (the
      clone footer) or not (CREATE2 for every known implementation and the ids in the
      transaction; the whole offline set when the collection is pinned): refuse.
    * C3: a clone bound to another chain: refuse (``owner()`` is the zero address here).
    * N6: a clone of another collection whose owner chain contains the moved token: refuse.
    * N4: a clone whose owner chain ends at the holder: allowed with the owner's card only.
    """
    from core.wallet import erc6551
    nested: List[Tuple[str, str]] = []
    ids_in_tx = calldata_ids(tx)
    for (contract, std, dest, token_id, _amt) in nft_out or ():
        d = str(dest or "").lower()
        if not d or int(d, 16) == 0 or d == _DEAD:
            continue  # a burn: no account to nest into
        c = str(contract).lower()
        what = f"{std} {contract} #{token_id}"
        try:
            impls = implementations(rpc, chain_id, c)
        except RuleReadError as exc:
            return Nesting(refusal=f"{exc} — the nesting rule cannot run for {what}; failing closed")
        own = account_addresses(chain_id, c, ids_in_tx | {int(token_id)}, impls)
        if d in own:
            return Nesting(refusal=(
                f"{what} would move into {dest}, the ERC-6551 account of its own collection's "
                f"#{own[d]} (deployed or not) — a token inside an account of its own collection "
                f"can own itself and is bricked forever"))
        for p in _pinned_profiles(chain_id):
            if p.address != c:
                continue
            for v in p.accounts:
                if d in erc6551.collection_account_addresses(
                        chain_id, p.address, p.max_supply, implementation=v.implementation,
                        salt=v.salt):
                    return Nesting(refusal=(
                        f"{what} would move into {dest}, the ERC-6551 account address of a "
                        f"token of its own collection (deployed or not) — a token inside such "
                        f"an account can own itself and is bricked forever"))
        try:
            clone = erc6551.read_clone(rpc, dest)
        except Exception as exc:  # noqa: BLE001
            return Nesting(refusal=(
                f"could not read the code at {dest}, the destination of {what} ({exc}) — a "
                f"token-bound account there could brick the token; failing closed"))
        if clone is None:
            continue
        _impl, _salt, f_chain, f_contract, f_id = clone
        if int(f_chain) != int(chain_id):
            return Nesting(refusal=(
                f"{dest} is a token-bound account of chain {f_chain}, not {chain} ({chain_id}) — "
                f"here its owner() is address(0), so {what} sent there is lost"))
        if str(f_contract).lower() == c:
            return Nesting(refusal=(
                f"{what} would move into {dest}, the token-bound account of {f_contract} "
                f"#{f_id} — a token inside an account of its own collection can own itself and "
                f"is bricked forever"))
        why, mine = _walk_to_holder(rpc, chain_id, (str(f_contract), int(f_id)), (c, int(token_id)),
                                    holder)
        if why:
            return Nesting(refusal=f"{what} into {dest}: {why}")
        if mine:
            nested.append((what, dest))
    return Nesting(nested=tuple(nested))


def call_nesting_refusal(chain_id: int, tx: Optional[dict]) -> Optional[str]:
    """A call TO a collection that names, in its calldata, an account of that same collection
    (``mint(to, …)`` / ``transferFrom(…, to, …)`` to someone else is no holder move, so no
    measured delta names it). Pure CREATE2, no RPC: the reviewed built-in implementations and
    the ids in the transaction, and a pinned collection's own versions over its offline set."""
    from core.wallet import account_kinds, erc6551
    to = str((tx or {}).get("to") or "").lower()
    named = calldata_addresses(tx)
    if not to or not named:
        return None
    impls = [(k.builtin_implementation.lower(), 0) for k in account_kinds.KINDS.values()
             if k.builtin_implementation]
    for p in _pinned_profiles(chain_id):
        if p.address == to:
            impls += [(str(v.implementation).lower(), int(v.salt)) for v in p.accounts]
            for v in p.accounts:
                hit = named & erc6551.collection_account_addresses(
                    chain_id, p.address, p.max_supply, implementation=v.implementation,
                    salt=v.salt)
                if hit:
                    return (f"a call to the pinned collection {to} names {sorted(hit)[0]}, the "
                            f"ERC-6551 account address of one of its own tokens (deployed or "
                            f"not) — a token inside such an account can own itself and is "
                            f"bricked forever")
    own = account_addresses(chain_id, to, calldata_ids(tx), impls)
    hit = sorted(named & set(own))
    if hit:
        return (f"a call to {to} names {hit[0]}, the ERC-6551 account of its own #{own[hit[0]]} "
                f"(deployed or not) — a token inside an account of its own collection can own "
                f"itself and is bricked forever")
    return None


# ---------------------------------------------------------------------------- D1

def _has_code_at(rpc: Rpc, address: str, block: int) -> bool:
    code = rpc("eth_getCode", [address, hex(block)])
    return isinstance(code, str) and len(code) > 2


def first_code_block(rpc: Rpc, address: str, head: int) -> int:
    """The first block with code at *address* (binary search). Raises when the node cannot
    answer a historical read (not an archive node)."""
    lo, hi = 0, head
    while lo < hi:
        mid = (lo + hi) // 2
        if _has_code_at(rpc, address, mid):
            hi = mid
        else:
            lo = mid + 1
    # A pruned node may answer "0x" for old state instead of an error, which pushes the search
    # late and makes the watch miss earlier arrivals. The block before the deploy must answer
    # "0x" AND the block must hold code; probe the genesis side too: a node that serves block
    # 1's state honestly serves any.
    if not _has_code_at(rpc, address, lo):
        raise RuntimeError(f"no code at block {lo}")
    if lo > 0:
        rpc("eth_getBalance", [address, hex(1)])  # raises on a node without old state
    return lo


def creation_block(rpc: Rpc, account: str, head: int) -> int:
    """The first block with code at *account* (binary search over ``eth_getCode``). Raises
    :class:`RuleReadError` when the node cannot answer a historical read."""
    try:
        return int(first_code_block(rpc, account, head))
    except Exception as exc:  # noqa: BLE001
        raise RuleReadError(f"the creation block of {account} could not be read ({exc}; an "
                            f"archive RPC answers it)")


def departure_refusal(rpc: Rpc, chain: str, chain_id: int, nft_out, *,
                      holder: Optional[str] = None, accepted: Sequence[str] = ()) -> Optional[str]:
    """124 D1 (069 v4 section 5 rule 5) for every NFT that leaves the holder, any collection:
    each deployed account of the token (every implementation :func:`implementations` knows)
    must have NO open approval — ERC-20 allowances, ERC-721 approvals, ApprovalForAll and
    Permit2 (``erc6551.open_approvals``, from the pin's ``deploy_block`` or else the account's
    own creation block read from the chain, to the head, every row live-confirmed). An account
    that is not deployed has granted nothing. A pinned collection's live code must also hash to
    its pin. Any failed read is an incomplete scan and refuses.

    Any contract can emit an ``Approval`` naming the account, so a row whose events no
    transaction SENT by an owner of the NFT emitted (``erc6551.attribute``) may be fabricated and
    cannot always be revoked. Only such a row may be accepted by the owner, by its key in
    *accepted* (the intent's ``accepted_unattributed_approvals``)."""
    from core.wallet import collection_registry, erc6551
    pinned = {p.address: p for p in _pinned_profiles(chain_id)}
    head = None
    for (contract, std, dest, token_id, _amt) in nft_out or ():
        c = str(contract).lower()
        d = str(dest or "").lower()
        if not d or int(d, 16) == 0 or d == _DEAD:
            continue   # a burn: no next owner inherits the account
        what = f"{std} {contract} #{token_id}"
        profile = pinned.get(c)
        if profile is not None:
            why = collection_registry.runtime_refusal(rpc, profile)
            if why:
                return why
            versions = [(v.implementation, v.salt) for v in profile.accounts]
        else:
            try:
                versions = implementations(rpc, chain_id, c)
            except RuleReadError as exc:
                return f"{exc} — the approval rule cannot run for {what}; failing closed"
        if "permit2" not in tuple(getattr(erc6551, "APPROVAL_KINDS", ())):
            return ("the approval scan does not cover Permit2 — an incomplete table is never "
                    "'empty'; failing closed")
        for impl, salt in versions:
            account = erc6551.account_address(chain_id, c, int(token_id), salt=int(salt),
                                              implementation=impl)
            try:
                if profile is None and not _has_code(rpc, account):
                    continue   # no account deployed: it granted nothing
                if head is None:
                    head = int(rpc("eth_blockNumber", []), 16)
                since = (int(profile.deploy_block) if profile is not None
                         else creation_block(rpc, account, head))
                rows = erc6551.open_approvals(rpc, account, since, to_block=head)
            except Exception as exc:  # noqa: BLE001
                return (f"the approval scan of {account}, the account of {what}, did not "
                        f"complete ({exc}) — an incomplete table is never 'empty', so the token "
                        f"may not leave; failing closed")
            if rows:
                try:
                    owners = erc6551.nft_owners(rpc, c, int(token_id), since, head)
                    if holder:
                        owners.add(str(holder).lower())
                    rows = erc6551.attribute(rpc, rows, owners)
                except Exception:  # noqa: BLE001 — origin unread: every row stays undecided
                    pass
                taken = {str(k).strip().lower() for k in accepted or ()}
                rows = [r for r in rows if r.attributed is True or r.key not in taken]
            if rows:
                return _open_approvals_text(what, account, rows)
    return None


def _open_approvals_text(what: str, account: str, rows) -> str:
    def _origin(r):
        if r.attributed is True:
            return ""
        why = ("no transaction an owner of this NFT sent emitted it"
               if r.attributed is False else "its origin could not be read")
        return f" [{why}; the owner may accept it as {r.key}]"
    shown = "; ".join(
        f"{r.kind} {r.contract}" + (f" #{r.token_id}" if r.token_id is not None else "")
        + f" → {r.spender}" + ("" if r.verified else " (unconfirmed)") + _origin(r)
        for r in rows[:5])
    more = f" (+{len(rows) - 5} more)" if len(rows) > 5 else ""
    return (f"{what} may not leave while its account {account} has {len(rows)} open "
            f"approval(s): {shown}{more}. Approvals survive a transfer of the NFT, so the next "
            f"owner's account could still be drained. Clear approvals first: "
            f"agent_nft_revoke_all, then send it")


# ---------------------------------------------------------------------------- R1

_ERC6551P_ACCOUNT_IID = "92ad233c"


def standing_approvals_refused(rpc: Rpc, account: str) -> bool:
    """The account's kind refuses standing approvals (ERC-6551P, proven by ERC-165 on its
    implementation); False for Tokenbound v3 and every unknown kind. Raises when unreadable."""
    from core.wallet import erc6551
    try:
        clone = erc6551.read_clone(rpc, account)
    except Exception as exc:  # noqa: BLE001
        raise RuleReadError(f"the code at {account} could not be read ({exc})")
    if clone is None:
        return False
    impl = str(clone[0])
    if impl.lower() == erc6551.ACCOUNT_V3_IMPL.lower():
        return False
    try:
        raw = _eth_call(rpc, impl, _SUPPORTS_INTERFACE + _ERC6551P_ACCOUNT_IID + "0" * 56)
    except Exception as exc:  # noqa: BLE001
        if "revert" in str(exc).lower():
            return False
        raise RuleReadError(f"supportsInterface of {impl} could not be read ({exc})")
    return int(str(raw or "0x0").removeprefix("0x") or "0", 16) == 1


def deposit_refusal(rpc: Rpc, chain: str, chain_id: int, intent, tx: Optional[dict], deltas,
                    holder: str, held: Optional[List[HeldAccount]] = None) -> Optional[str]:
    """124 R1: before the treasury puts anything into an account it holds whose kind may keep
    standing approvals (Tokenbound v3, an unknown kind), scan that account's approvals; an open
    row — the earlier owner's grant survives a sale — refuses with ``agent_nft_revoke_all``. An
    ERC-6551P account refuses standing approvals by construction and is not scanned. An
    unreadable holdings state checks nothing (logged); an unreadable account refuses."""
    from core.wallet import erc6551
    held = held_accounts() if held is None else held
    mine = {h.account: h for h in held or () if h.chain == chain}
    if not mine:
        return None
    dests = {str(d).lower() for (_t, d, _a) in getattr(deltas, "holder_transfers", ()) or ()}
    dests |= {str(d).lower() for (_c, _s, d, _i, _a) in getattr(deltas, "holder_nft_out", ()) or ()}
    if int((tx or {}).get("value") or 0) > 0:
        dests.add(str((tx or {}).get("to") or "").lower())
    for account in sorted(dests & set(mine)):
        h = mine[account]
        what = f"{h.label or h.collection} #{h.token_id}"
        try:
            if standing_approvals_refused(rpc, account):
                continue
            head = int(rpc("eth_blockNumber", []), 16)
            rows = erc6551.open_approvals(rpc, account, creation_block(rpc, account, head),
                                          to_block=head)
        except Exception as exc:  # noqa: BLE001
            return (f"the approval scan of {account}, the account of {what}, did not complete "
                    f"({exc}) — a deposit waits until it can be read; failing closed")
        if rows:
            shown = "; ".join(f"{r.kind} {r.contract} → {r.spender}" for r in rows[:3])
            return (f"{account}, the account of {what}, has {len(rows)} open approval(s) ({shown}) "
                    f"— an earlier owner's grant survives a sale and could drain what goes in. "
                    f"Clear them first: agent_nft_revoke_all, then deposit")
    return None


# ---------------------------------------------------------------------------- M1

def _pinned_held(chain: str, h: HeldAccount, rpc: Optional[Rpc], holder: str) -> bool:
    """The security review (2026-11-20): a held row is internal only for a PINNED collection,
    re-read live — the collection's ``ownerOf`` still names the treasury and the account is
    that token's own. A collection nobody pinned can lie in ``ownerOf`` (and take the account
    back later), so a send to its account is charged in full."""
    try:
        from core.wallet import collection_registry
        p = collection_registry.profile_for(collection_registry.chain_id_of(chain), h.collection)
        if p is None or rpc is None:
            return False
        if p.version_for_account(int(h.token_id), h.account) is None:
            return False
        return owner_of(rpc, p.address, int(h.token_id)) == str(holder).lower()
    except Exception:  # noqa: BLE001 — unreadable: charged in full
        return False


def internal_move(chain: str, intent, tx: Optional[dict], deltas, holder: str,
                  sender: Optional[str] = None,
                  held: Optional[List[HeldAccount]] = None, rpc: Optional[Rpc] = None) -> bool:
    """124 M1 (D5): True when the transaction only moves value between the treasury and an NFT
    account it holds on *chain*: an account paying its owner (``via_account``, the declared
    destination is the treasury — the treasury-side rule already allowed only that), or the
    treasury sending native value or one ERC-20 ``transfer`` whose every measured recipient is
    the account of a held NFT of a PINNED collection that the chain still shows as the
    treasury's (``ownerOf``, read now with *rpc*). Anything else is not internal."""
    if getattr(intent, "via_account", None):
        return bool(sender) and str(getattr(intent, "to", "") or "").lower() == str(sender).lower() \
            and not getattr(deltas, "holder_nft_out", ()) and not getattr(intent, "is_nft_op", False)
    if any(getattr(intent, f, False) for f in (
            "is_nft_op", "is_claim", "is_deploy", "is_registration", "is_allowance_op",
            "is_liquidity_op", "is_collection_mint", "is_collection_reveal", "is_resource_call")):
        return False
    held = held_accounts() if held is None else held
    mine = {h.account for h in held or () if h.chain == chain and _pinned_held(chain, h, rpc, holder)}
    if not mine:
        return False
    to = str((tx or {}).get("to") or "").lower()
    data = str((tx or {}).get("data") or "0x").lower()
    outs = [str(d).lower() for (_t, d, _a) in getattr(deltas, "holder_transfers", ()) or ()]
    if data in ("", "0x"):
        return to in mine and not outs
    if data[:10] == "0xa9059cbb" and outs and all(d in mine for d in outs):
        return int((tx or {}).get("value") or 0) == 0
    return False


# ---------------------------------------------------------------------------- S3

def signature_speaks_for(held: Optional[List[HeldAccount]] = None,
                         kinds: Optional[Dict[str, str]] = None) -> List[str]:
    """124 S3: the NFT accounts a treasury signature ALSO speaks for — a held account whose kind
    accepts the owner's signature as its own (ERC-1271; Tokenbound v3). One line each; empty
    when there is none or the holdings cannot be read (the read is logged)."""
    from core.wallet.account_kinds import kind_named
    if held is None:
        try:
            from core.wallet import holdings_state
            rows = holdings_state.tracked()
        except Exception as exc:  # noqa: BLE001
            logger.warning("nft_rules: holdings unreadable for the signature note (%s)", exc)
            return []
    else:
        rows = [{"account": h.account, "chain": h.chain, "label": h.label,
                 "kind": (kinds or {}).get(h.account, "tokenbound_v3")} for h in held]
    out = []
    for r in rows:
        kind = kind_named(str(r.get("kind") or "tokenbound_v3"))
        if kind is not None and kind.signs_as_owner and r.get("account"):
            out.append(f"this signature also speaks for {r.get('label') or 'an NFT'}'s account "
                       f"{r['account']} on {r.get('chain')} ({kind.label}: ERC-1271 accepts the "
                       f"owner's signature as the account's)")
    return out


def unpinned_market(chain: str, tx, rpc) -> Optional[Tuple[str, object]]:
    """124 W4: ``(market, profile)`` when *tx* is a buyer call on the market of an UNPINNED
    ERC-6551P collection — the listing's or offer's collection is read from the chain
    (``collection_registry.chain_profile``) and its ``MARKET()`` must be the called address.
    None otherwise (the gate then sees no market). Raises nothing."""
    import core.wallet.sale_conditions as S
    to = str((tx or {}).get("to") or "").lower()
    data = str((tx or {}).get("data") or "").lower()
    if not to or data[:10] not in (S.BUY_SELECTOR, S.ACCEPT_SELECTOR):
        return None
    try:
        if data[:10] == S.BUY_SELECTOR:
            collection = S.decode_buy(data).listing.collection
        else:
            import core.wallet.sale_orders as O
            collection = O.decode_accept(data).offer.collection
        from core.wallet import collection_registry
        profile = collection_registry.chain_profile(chain, collection, rpc=rpc)
    except Exception:  # noqa: BLE001 — not a market this build can read: no market
        return None
    std = getattr(profile, "erc6551p", None)
    if std is None or str(std.market).lower() != to:
        return None
    return to, profile


# ---------------------------------------------------------------------------- F1, F2

def future_account_refusal(rpc: Rpc, chain_id: int, intent, tx: Optional[dict], deltas,
                           holder: str) -> Optional[str]:
    """124 F1/F2 (C5): no asset of any kind into the account of a token that is not minted yet —
    whoever mints it later owns what sits there. The candidates are every address the
    transaction names; the collections are each collection whose NFT the transaction moves and
    every pinned one; the ids are the ids in
    the transaction (and a pinned collection's offline set). A failed ``ownerOf`` refuses."""
    from core.wallet import erc6551
    cands = transaction_addresses(intent, tx, deltas) - {str(holder).lower()}
    if not cands:
        return None
    ids = calldata_ids(tx)
    ids.update(int(i) for (_c, _s, _d, i, _a) in getattr(deltas, "holder_nft_out", ()) or ())
    ids.update(int(i) for (_c, _s, _d, i, _a) in getattr(deltas, "holder_nft_in", ()) or ())
    moved = {str(c).lower() for (c, _s, _d, _i, _a) in
             tuple(getattr(deltas, "holder_nft_out", ()) or ())
             + tuple(getattr(deltas, "holder_nft_in", ()) or ())}
    hits: Dict[str, Tuple[str, int]] = {}
    for coll in sorted(moved - {""}):
        try:
            impls = implementations(rpc, chain_id, coll)
        except RuleReadError as exc:
            return f"{exc} — the future-account rule cannot run; failing closed"
        for addr, k in account_addresses(chain_id, coll, ids, impls).items():
            if addr in cands:
                hits.setdefault(addr, (coll, k))
    for p in _pinned_profiles(chain_id):
        for addr in sorted(cands - set(hits)):
            for v in p.accounts:
                k = erc6551.collection_account_token_id(chain_id, p.address, p.max_supply, addr,
                                                        implementation=v.implementation,
                                                        salt=v.salt)
                if k is not None:
                    hits.setdefault(addr, (p.address, k))
    for addr, (coll, k) in sorted(hits.items()):
        try:
            minted = owner_of(rpc, coll, k) is not None
            why = ""
        except RuleReadError as exc:
            minted, why = False, f" ({exc})"
        if not minted:
            return (f"the transaction names {addr}, the ERC-6551 account of {coll} #{k}, a token "
                    f"not minted yet{why} — whoever mints #{k} later owns every asset sent "
                    f"there; failing closed")
    return None


# ---------------------------------------------------------------------------- 5.4 the generic call

#: 124 section 5.4: the known selectors of an ERC-721 sale, market or collection, and the typed
#: verb that makes each call with its checks. ``True`` = the shape names an NFT market by itself
#: (a struct argument no other contract shares); ``False`` = the target must prove it is a
#: collection (ERC-165) or a sale or market (it answers ``owed(address)``).
NFT_SELECTORS: Dict[str, Tuple[str, bool]] = {
    "0x156e29f6": ("agent_nft_collection_mint", False),    # mint(address,uint256,uint256)
    "0x1e29517f": ("agent_nft_collection_mint", True),     # mintWL(address,uint256,uint256,uint256,bytes32[])
    "0xb93f208a": ("agent_nft_collection_reveal", False),  # reveal(uint256[])
    "0x1e83409a": ("agent_nft_sale_claim or agent_nft_market_claim", False),  # claim(address)
    "0x9b2db373": ("agent_nft_sale_settle", False),        # settle(uint256[])
    "0xe9c3b815": ("agent_nft_buy", True),                 # buy(Listing,…)
    "0xec48f34c": ("agent_nft_buy", True),                 # checkedFill(…)
    "0x8e431f0c": ("agent_nft_sale_accept", True),         # accept(Offer,…)
    "0x4b8f6ae4": ("agent_nft_sale_ready", False),         # readyToSell(uint256)
    "0x80437f23": ("agent_nft_sale_unready", False),       # cancelReady(uint256)
    "0x40e58ee5": ("agent_nft_sale_cancel_listing", False),  # cancel(uint256)
}
_ERC721_IID = "80ac58cd"
_SUPPORTS_INTERFACE = "0x01ffc9a7"
_OWED = "0xdf18e047"


def _answers(rpc: Rpc, to: str, data: str) -> bool:
    """True when the view answers a non-empty word; False on a revert or empty data. An RPC
    failure RAISES :class:`RuleReadError`."""
    try:
        raw = _eth_call(rpc, to, data)
    except Exception as exc:  # noqa: BLE001
        if "revert" in str(exc).lower():
            return False
        raise RuleReadError(f"a view of {to} could not be read ({exc})")
    return len(str(raw or "").removeprefix("0x")) >= 64


def is_nft_contract(rpc: Rpc, to: str) -> bool:
    """*to* is an ERC-721 collection (ERC-165) or a sale or market (it answers ``owed``)."""
    try:
        raw = _eth_call(rpc, to, _SUPPORTS_INTERFACE + _ERC721_IID + "0" * 56)
        if int(str(raw or "0x0").removeprefix("0x") or "0", 16) == 1:
            return True
    except Exception as exc:  # noqa: BLE001
        if "revert" not in str(exc).lower():
            raise RuleReadError(f"supportsInterface of {to} could not be read ({exc})")
    return _answers(rpc, to, _OWED + "0" * 64)


def typed_verb_refusal(rpc: Rpc, to: Optional[str], data: Optional[str]) -> Optional[str]:
    """124 section 5.4: a generic call (``defi_trade_call``, a dapp page) that makes a known
    mint, reveal, claim, settle, buy, accept or sale act of an ERC-721 collection, sale or market
    refuses and names the typed verb, which checks what the call does. The checked path is never
    more blocked than the unchecked one. Any other call passes. An unreadable target refuses."""
    sel = str(data or "").lower()[:10]
    row = NFT_SELECTORS.get(sel)
    if row is None or not to:
        return None
    verb, by_shape = row
    if not by_shape:
        try:
            if not is_nft_contract(rpc, str(to)):
                return None
        except RuleReadError as exc:
            return (f"{sel} is a known NFT sale or market call, and {exc} — I cannot tell "
                    f"whether {to} is one; failing closed. Use {verb}")
    return (f"{to} is an NFT collection, sale or market, and {sel} is one of its known calls — "
            f"use {verb}, which checks what arrives, what is paid and that nothing else moves; "
            f"never a generic call")


__all__ = ["HeldAccount", "MAX_WALK", "Nesting", "RuleReadError", "account_addresses",
           "account_implementation", "call_nesting_refusal", "calldata_addresses", "creation_block",
           "departure_refusal", "deposit_refusal", "first_code_block", "calldata_ids", "cross_chain_refusal",
           "future_account_refusal", "held_accounts", "implementations", "internal_move",
           "is_nft_contract", "nesting", "owner_of",
           "signature_speaks_for", "standing_approvals_refused", "transaction_addresses", "typed_verb_refusal", "unpinned_market"]
