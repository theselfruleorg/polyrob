"""Solana transaction inspection + the simulate call that observes it.

`core/wallet/solana_simulation.py` is the PURE half — it turns a
`simulateTransaction` result into asserted deltas. This module is the half that
decides *what the simulation is even allowed to look at*, and it exists because
two structural gaps made the Solana guard weaker than its EVM twin:

## 1. The observation set was the caller's own declaration

`simulateTransaction` returns post-state only for the accounts you NAME. The
first implementation named `[owner, ATA(token_in), ATA(token_out)]` — the three
accounts the caller declared. An extra instruction touching a DIFFERENT
wallet-owned token account (an SPL `Approve` delegate grant, a `SetAuthority`,
a `CloseAccount`) was therefore invisible: `parse_deltas` saw no state for it,
so its taxonomy never fired, and the bytes broadcast were byte-identical to the
bytes simulated. The EVM guard has no such hole — it scans the whole
transaction's event log.

`parse_deltas` only ever counts an account whose parsed `owner` is OURS, so
"observe everything that matters" reduces exactly to "name every token account
the wallet owns". That is enumerable in one RPC call per token program, which
is what `simulation_addresses` does — independent of whether the transaction
reaches the account through a static key or an address-lookup table.

## 2. The ATA was derived under one token program

The associated-token address is a PDA over `(owner, token_program, mint)`, so a
Token-2022 mint has a DIFFERENT associated account than the classic SPL Token
program produces. Deriving only the classic one meant a Token-2022 mint —
which Jupiter happily routes — resolved to an address that does not exist, the
declared mint's delta was never observed, and the "more is leaving than you
declared" assertion was SKIPPED rather than triggered. The mint's OWNER program
is read from the chain here; when that read fails, BOTH candidates are named
(naming a nonexistent account is free — it simply reads back as null).

## The program allowlist, and the instructions it cannot see

Defense in depth, not a replacement for the delta assertions: the threat model
is a malicious or buggy *aggregator response*, i.e. an attacker who controls the
transaction bytes but not a deployed program. A program id can never come from
an address-lookup table (the runtime requires it in the static keys), so
enumerating the top-level program ids is complete FOR THE PROGRAM SET, and an
unrecognized program REFUSES.

⚠️ The program set is NOT the attack surface on its own (CR-H05). An injected
`Approve`/`SetAuthority`/`CloseAccount`/`FreezeAccount` is an instruction of
the SPL Token program, which every swap legitimately calls — an allowlist of
programs lets it straight through. So every top-level token and System
instruction is also DECODED (`instruction_refusal`): an authority grant, a
freeze or an `Assign` on any account the transaction did not itself create is
refused, a `CloseAccount` whose rent and balance go anywhere but the owner is
refused, and so is the durable-nonce family (CR-M05 — a nonce transaction
never expires, so the blockhash no longer bounds replay). An account reached
through a lookup table cannot be resolved from the bytes, so a dangerous
instruction that names one refuses too.

The simulation is the second layer, and it observes EVERY token account the
wallet owns: the RPC caps how many accounts one `simulateTransaction` may
report, so the observation is SPLIT across as many calls as it needs, and a
wallet whose accounts cannot be enumerated refuses rather than simulating with
a narrower view (CR-H05).

What this does NOT cover, stated plainly: instructions reached by CPI from an
allowed program (Jupiter routing into a DEX) are not decodable from the
transaction — the delta + authority assertions remain the control for what
those actually do.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple

from core.wallet.solana_simulation import SPL_TOKEN_PROGRAMS, SolanaDeltas, parse_deltas

logger = logging.getLogger(__name__)

ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"

#: Jupiter's aggregator program versions. A route is built by Jupiter's HTTP
#: API but EXECUTED by one of these on-chain programs.
JUPITER_PROGRAM_IDS = frozenset({
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",          # v6
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",          # v4
})

#: The SPL memo programs. Harmless (they write a log line) but they do turn up
#: in aggregator transactions, and an unrecognized program is a refusal.
MEMO_PROGRAM_IDS = frozenset({
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",          # v3
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",          # v1
})

#: Top-level programs a Jupiter swap legitimately calls: compute-budget hints,
#: wrapping SOL (System) and closing the wrapper (Token), creating the
#: destination associated account, the route itself, and an optional memo.
#: Relay.link's deposit program (037). Deliberately NOT in
#: :data:`BASE_ALLOWED_PROGRAMS`: a SWAP that suddenly calls it is exactly the
#: anomaly the allowlist exists to catch, so only the bridge verb may pass it in
#: via ``extra_allowed``. PINNED here rather than read from the quote — taking
#: the program id from the same untrusted payload we are vetting would make the
#: check vacuous. Verified on mainnet 2026-09-11: exists, ``executable=true``,
#: owned by the BPF upgradeable loader.
RELAY_PROGRAM_IDS = frozenset({"99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2"})

BASE_ALLOWED_PROGRAMS = frozenset(
    {SYSTEM_PROGRAM_ID, COMPUTE_BUDGET_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID}
    | set(SPL_TOKEN_PROGRAMS) | set(JUPITER_PROGRAM_IDS) | set(MEMO_PROGRAM_IDS)
)

#: How many accounts the simulation may ask state for.
#:
#: ⚠️ Measured, not theoretical (2026-09-11, prod): `getMultipleAccounts` refuses
#: more than 100 keys, but `simulateTransaction` on the pinned RPC (Alchemy)
#: refuses more than **5** — `{'code': -32602, 'message': 'Too many accounts
#: provided; max 5'}`. Asking for more does not truncate, it FAILS the whole
#: simulation, and a failed simulation refuses the trade outright. A truncated
#: simulation with the loud warning below is strictly better than none: the
#: owner and the DECLARED mints are ordered FIRST, so the assertions that bound
#: the trade are the ones that survive.
#:
#: Raise it with `DEFI_SOLANA_SIM_MAX_ACCOUNTS` on an RPC that allows more —
#: fuller observation is better when the provider will serve it.
def sim_max_addresses() -> int:
    """The per-call account cap, resolved at CALL time (never frozen at import
    so an operator can retune it without a redeploy)."""
    from core.env import int_env
    return max(1, int_env("DEFI_SOLANA_SIM_MAX_ACCOUNTS", 5))


#: Back-compat alias for the historical constant. Reads the default; every
#: internal caller uses `sim_max_addresses()`.
MAX_SIM_ADDRESSES = 5


def extra_allowed_programs() -> frozenset:
    """Operator-added program ids (`DEFI_SOLANA_PROGRAM_ALLOWLIST_EXTRA`).

    The escape hatch for the day an aggregator ships a new program version: an
    unrecognized program REFUSES the swap, and without an owner-controlled
    widening the only remedy would be a code deploy. Deliberately additive
    only — nothing here can remove a check.
    """
    raw = (os.getenv("DEFI_SOLANA_PROGRAM_ALLOWLIST_EXTRA", "") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def allowed_programs() -> frozenset:
    return BASE_ALLOWED_PROGRAMS | extra_allowed_programs()


#: `getMultipleAccounts` refuses more than 100 keys, and the pre-state read is
#: ONE call — so this bounds the whole observation set, not one simulation.
MAX_OBSERVED_ACCOUNTS = 100

#: The fee (base + priority) a transaction may carry. The old whole-native
#: bound (`is_plausible_rent`, 0.01 SOL) already refused a larger one for every
#: token swap; this makes the bound explicit for every verb. A priority fee is
#: paid out of the wallet like any other outflow.
MAX_FEE_LAMPORTS = 10_000_000

# System program instruction tags (u32 little-endian).
_SYS_CREATE_ACCOUNT = 0
_SYS_ASSIGN = 1
_SYS_TRANSFER = 2
_SYS_CREATE_ACCOUNT_WITH_SEED = 3
_SYS_ALLOCATE = 8
_SYS_ALLOCATE_WITH_SEED = 9
_SYS_ASSIGN_WITH_SEED = 10
_SYS_TRANSFER_WITH_SEED = 11
#: AdvanceNonceAccount, WithdrawNonceAccount, InitializeNonceAccount,
#: AuthorizeNonceAccount, UpgradeNonceAccount.
_SYS_NONCE_TAGS = frozenset({4, 5, 6, 7, 12})
_SYS_REASSIGN_TAGS = frozenset({_SYS_ASSIGN, _SYS_ASSIGN_WITH_SEED,
                                _SYS_ALLOCATE, _SYS_ALLOCATE_WITH_SEED})
_SYS_VALUE_MOVE_TAGS = frozenset({_SYS_CREATE_ACCOUNT, _SYS_TRANSFER,
                                  _SYS_CREATE_ACCOUNT_WITH_SEED,
                                  _SYS_TRANSFER_WITH_SEED})

# SPL Token / Token-2022 instruction tags (u8).
_TOK_APPROVE = 4
_TOK_SET_AUTHORITY = 6
_TOK_CLOSE_ACCOUNT = 9
_TOK_FREEZE_ACCOUNT = 10
_TOK_APPROVE_CHECKED = 13
_TOK_GRANT_TAGS = {_TOK_APPROVE: "Approve", _TOK_APPROVE_CHECKED: "ApproveChecked",
                   _TOK_SET_AUTHORITY: "SetAuthority",
                   _TOK_FREEZE_ACCOUNT: "FreezeAccount"}

_COMPUTE_SET_LIMIT = 2
_COMPUTE_SET_PRICE = 3
_DEFAULT_CU_PER_IX = 200_000
_MAX_CU = 1_400_000
_BASE_FEE_PER_SIGNATURE = 5_000


class ObservationError(RuntimeError):
    """The observation set could not be established. The caller refuses."""


# --------------------------------------------------------------------------
# Associated token accounts
# --------------------------------------------------------------------------

def derive_ata(owner: str, mint: str, token_program: str) -> Optional[str]:
    """The associated token account for ``(owner, token_program, mint)``.

    None on any failure — the caller must treat that as "not observed", never
    as "nothing there".
    """
    try:
        from solders.pubkey import Pubkey
        ata_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
        addr, _ = Pubkey.find_program_address(
            [bytes(Pubkey.from_string(owner)),
             bytes(Pubkey.from_string(token_program)),
             bytes(Pubkey.from_string(mint))], ata_program)
        return str(addr)
    except Exception:
        return None


def candidate_atas(owner: str, mint: str) -> Tuple[str, ...]:
    """The associated account under EVERY known token program.

    Used when the mint's owning program cannot be read. Naming an address that
    does not exist costs nothing (it reads back null); NOT naming the right one
    silently disables the outflow assertion.
    """
    out: List[str] = []
    for program in sorted(SPL_TOKEN_PROGRAMS):
        ata = derive_ata(owner, mint, program)
        if ata and ata not in out:
            out.append(ata)
    return tuple(out)


def mint_token_program(mint: str, rpc: Callable) -> Optional[str]:
    """The token program that OWNS *mint* (classic SPL Token or Token-2022).

    Read from the chain, never assumed: the two produce different associated
    addresses for the same owner+mint. None when the account cannot be read or
    is owned by something that is not a token program.
    """
    try:
        res = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    except Exception as exc:
        logger.warning("solana: could not read the token program for mint %s "
                       "(%s) — deriving both candidate accounts", mint, exc)
        return None
    value = (res or {}).get("value") if isinstance(res, dict) else None
    program = (value or {}).get("owner") if isinstance(value, dict) else None
    if program and str(program) in SPL_TOKEN_PROGRAMS:
        return str(program)
    return None


def atas_for_mint(owner: str, mint: str, rpc: Optional[Callable] = None) -> Tuple[str, ...]:
    """Every associated account worth observing for *mint*.

    One entry when the mint's token program is known; both candidates when it
    is not. Never empty unless the derivation itself failed.
    """
    program = mint_token_program(mint, rpc) if rpc is not None else None
    if program:
        ata = derive_ata(owner, mint, program)
        if ata:
            return (ata,)
    return candidate_atas(owner, mint)


def owned_token_accounts(owner: str, rpc: Callable, *,
                         strict: bool = False) -> Tuple[str, ...]:
    """Every EXISTING token account the wallet owns, across both programs.

    This is what closes the "the simulation only watched the accounts the
    caller declared" hole: whatever route the transaction takes to reach one of
    these — a static key or an address-lookup table — naming it here means the
    simulation reports its state and `parse_deltas` can see a drain, a delegate
    grant or an authority change on it.

    ``strict=True`` (the money path, CR-H05) RAISES :class:`ObservationError`
    when either program cannot be enumerated: a narrower observation set is an
    unobserved account, and simulating with it would pass a drain on exactly
    the account we failed to list.
    """
    found: List[str] = []
    for program in sorted(SPL_TOKEN_PROGRAMS):
        try:
            res = rpc("getTokenAccountsByOwner",
                      [owner, {"programId": program}, {"encoding": "jsonParsed"}])
        except Exception as exc:
            if strict:
                raise ObservationError(
                    f"could not enumerate the wallet's {program} accounts "
                    f"({exc})") from exc
            logger.warning("solana: could not enumerate %s accounts for %s (%s) "
                           "— the observation set is narrower than intended",
                           program, owner, exc)
            continue
        if strict and not (isinstance(res, dict)
                           and isinstance(res.get("value"), list)):
            raise ObservationError(
                f"the RPC returned no account list for the wallet's {program} "
                f"accounts")
        for entry in ((res or {}).get("value") or []) if isinstance(res, dict) else []:
            pubkey = entry.get("pubkey") if isinstance(entry, dict) else None
            if pubkey and str(pubkey) not in found:
                found.append(str(pubkey))
    return tuple(found)


# --------------------------------------------------------------------------
# Transaction decode + program allowlist
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TxInspection:
    """What the raw transaction bytes say about themselves.

    ``ok=False`` is a REFUSAL, never a shrug: bytes we cannot decode are bytes
    we cannot vet, and they are about to be signed.
    """
    ok: bool
    reason: str = ""
    program_ids: Tuple[str, ...] = ()
    account_keys: Tuple[str, ...] = ()
    unknown_programs: Tuple[str, ...] = ()
    #: Every top-level instruction, decoded (CR-H05).
    instructions: Tuple["DecodedIx", ...] = ()
    num_signatures: int = 0


@dataclass(frozen=True)
class DecodedIx:
    """One top-level instruction. An account reached through an address-lookup
    table cannot be resolved from the bytes and is ``None`` — never guessed."""
    program: str
    data: bytes
    accounts: Tuple[Optional[str], ...]


def _system_tag(data: bytes) -> Optional[int]:
    return int.from_bytes(data[:4], "little") if len(data) >= 4 else None


def _account(ix: DecodedIx, position: int) -> Optional[str]:
    return ix.accounts[position] if position < len(ix.accounts) else None


def created_accounts(instructions: Sequence[DecodedIx], *,
                     funder: Optional[str] = None,
                     strict: bool = True) -> Tuple[str, ...]:
    """Accounts the transaction itself brings into existence.

    ``strict`` keeps only creations that FAIL on an existing account (System
    ``CreateAccount``/``CreateAccountWithSeed``, the associated-token
    ``Create``) — the proof an account is new. ``strict=False`` adds
    ``CreateIdempotent``, which is a no-op on an existing account: fine for
    "observe it", never for "an authority grant on it is harmless".
    ``funder`` keeps only the ones that account pays for.
    """
    out: List[str] = []
    for ix in instructions:
        new = None
        if ix.program == SYSTEM_PROGRAM_ID and _system_tag(ix.data) in (
                _SYS_CREATE_ACCOUNT, _SYS_CREATE_ACCOUNT_WITH_SEED):
            new = _account(ix, 1)
        elif ix.program == ASSOCIATED_TOKEN_PROGRAM_ID:
            if ix.data in (b"", b"\x00") or (not strict and ix.data == b"\x01"):
                new = _account(ix, 1)
        if not new:
            continue
        if funder is not None and _account(ix, 0) != funder:
            continue
        if new not in out:
            out.append(new)
    return tuple(out)


def fee_lamports(instructions: Sequence[DecodedIx], num_signatures: int) -> int:
    """Base fee plus the ComputeBudget priority fee, in lamports.

    The unit limit defaults the way the runtime's did (200k per non-budget
    instruction, capped at 1.4M) when no ``SetComputeUnitLimit`` is present —
    an over-estimate on newer runtimes, which only makes the excess smaller.
    """
    price = 0
    limit: Optional[int] = None
    others = 0
    for ix in instructions:
        if ix.program == COMPUTE_BUDGET_PROGRAM_ID:
            tag = ix.data[:1]
            if tag == bytes([_COMPUTE_SET_LIMIT]) and len(ix.data) >= 5:
                limit = int.from_bytes(ix.data[1:5], "little")
            elif tag == bytes([_COMPUTE_SET_PRICE]) and len(ix.data) >= 9:
                price = int.from_bytes(ix.data[1:9], "little")
        else:
            others += 1
    if limit is None:
        limit = _DEFAULT_CU_PER_IX * others
    limit = min(int(limit), _MAX_CU)
    priority = (int(price) * limit + 999_999) // 1_000_000
    return _BASE_FEE_PER_SIGNATURE * max(1, int(num_signatures)) + priority


def instruction_refusal(instructions: Sequence[DecodedIx], *,
                        owner: str) -> Optional[str]:
    """Why the decoded instructions must not be signed, or None (CR-H05/M05).

    Refused, at the TOP level:

    * the durable-nonce family — a nonce transaction does not expire, so a
      signed swap would stay valid indefinitely;
    * ``Assign``/``Allocate`` on an account this transaction did not create —
      re-owning the wallet's own system account hands every lamport over;
    * ``Approve``/``ApproveChecked``/``SetAuthority``/``FreezeAccount`` on any
      account this transaction did not create — a standing grant over a
      position the swap does not spend;
    * ``CloseAccount`` whose rent and balance go to anyone but the owner.

    A dangerous instruction whose target comes from an address-lookup table
    cannot be vetted and refuses.
    """
    created = set(created_accounts(instructions, strict=True))
    for n, ix in enumerate(instructions):
        if ix.program == SYSTEM_PROGRAM_ID:
            tag = _system_tag(ix.data)
            if tag in _SYS_NONCE_TAGS:
                return (f"instruction #{n} is a System durable-nonce instruction "
                        f"(tag {tag}). A nonce transaction never expires, so the "
                        f"recent blockhash no longer bounds replay — a signed "
                        f"transaction could be broadcast at any later time")
            if tag in _SYS_REASSIGN_TAGS:
                target = _account(ix, 0)
                if target is None or target not in created:
                    return (f"instruction #{n} re-assigns or re-allocates "
                            f"{target or 'a lookup-table account'}, which this "
                            f"transaction did not create — that hands the "
                            f"account's lamports to another program")
        elif ix.program in SPL_TOKEN_PROGRAMS and ix.data:
            tag = ix.data[0]
            if tag in _TOK_GRANT_TAGS:
                target = _account(ix, 0)
                if target is None or target not in created:
                    return (f"instruction #{n} is a token {_TOK_GRANT_TAGS[tag]} "
                            f"on {target or 'a lookup-table account'}, which "
                            f"this transaction did not create. An authority "
                            f"grant or freeze over an existing account is a "
                            f"future drain the transaction itself does not "
                            f"perform")
            elif tag == _TOK_CLOSE_ACCOUNT:
                dest = _account(ix, 1)
                if dest != owner:
                    return (f"instruction #{n} closes a token account and sends "
                            f"its balance and rent to "
                            f"{dest or 'a lookup-table account'}, not to this "
                            f"wallet ({owner})")
    return None


def system_value_move_refusal(instructions: Sequence[DecodedIx]) -> Optional[str]:
    """CR-M04: a top-level System ``Transfer``/``TransferWithSeed``/
    ``CreateAccount``/``CreateAccountWithSeed``, or None.

    For a verb whose deposit is made BY a pinned program (the Relay bridge),
    such an instruction can only move SOL to a destination that program did not
    choose — i.e. not a Relay deposit.
    """
    for n, ix in enumerate(instructions):
        if ix.program == SYSTEM_PROGRAM_ID and \
                _system_tag(ix.data) in _SYS_VALUE_MOVE_TAGS:
            dest = _account(ix, 1)
            return (f"instruction #{n} is a top-level System transfer/create "
                    f"to {dest or 'a lookup-table account'}. The deposit is "
                    f"made by the pinned Relay program itself, so a direct SOL "
                    f"move is not a Relay deposit")
    return None


def inspect_transaction(raw_tx: Any, *,
                       extra_allowed: frozenset = frozenset(),
                       owner: Optional[str] = None) -> TxInspection:
    """Decode *raw_tx*, vet its TOP-LEVEL program ids against the allowlist,
    and decode its token and System instructions (:func:`instruction_refusal`).

    ``extra_allowed`` is a CALLER-scoped widening for a verb that legitimately
    calls a program the default set excludes (037: the bridge calls Relay's
    deposit program). It is additive, per-call, and must only ever be passed a
    PINNED constant — never a value read from the payload being vetted.

    ``owner`` defaults to the fee payer (static key 0), which the signer
    already requires to be this wallet.
    """
    try:
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(bytes(raw_tx))
        message = tx.message
        keys = tuple(str(k) for k in message.account_keys)
        programs: List[str] = []
        decoded: List[DecodedIx] = []
        for ix in message.instructions:
            index = int(ix.program_id_index)
            if index < 0 or index >= len(keys):
                return TxInspection(
                    False,
                    f"an instruction names program index {index}, which is "
                    f"outside the transaction's {len(keys)} account keys — the "
                    f"transaction is malformed",
                    account_keys=keys)
            if keys[index] not in programs:
                programs.append(keys[index])
            decoded.append(DecodedIx(
                program=keys[index], data=bytes(ix.data),
                accounts=tuple(keys[int(i)] if int(i) < len(keys) else None
                               for i in bytes(ix.accounts))))
        num_signatures = int(message.header.num_required_signatures)
    except Exception as exc:
        return TxInspection(
            False, f"the transaction bytes could not be decoded ({exc})")

    allowed = allowed_programs() | frozenset(extra_allowed or ())
    unknown = tuple(p for p in programs if p not in allowed)
    if unknown:
        return TxInspection(
            False,
            f"the transaction calls {len(unknown)} unrecognized program(s) "
            f"{list(unknown)}. A swap route calls the aggregator, the token "
            f"programs, the associated-token program, System and "
            f"ComputeBudget — nothing else. An unrecognized program is exactly "
            f"where an injected authority grant or account close would sit, so "
            f"this REFUSES rather than trusting the delta check to catch it "
            f"(widen with DEFI_SOLANA_PROGRAM_ALLOWLIST_EXTRA if this is a "
            f"legitimate new aggregator version)",
            program_ids=tuple(programs), account_keys=keys,
            unknown_programs=unknown)
    payer = owner or (keys[0] if keys else "")
    refusal = instruction_refusal(decoded, owner=payer)
    if refusal is None:
        fee = fee_lamports(decoded, num_signatures)
        if fee > MAX_FEE_LAMPORTS:
            refusal = (f"the transaction carries a {fee}-lamport fee, above the "
                       f"{MAX_FEE_LAMPORTS} a transaction may pay — a priority "
                       f"fee is money leaving the wallet like any other")
    if refusal:
        return TxInspection(
            False, f"REFUSED: {refusal}. Nothing was signed.",
            program_ids=tuple(programs), account_keys=keys,
            instructions=tuple(decoded), num_signatures=num_signatures)
    return TxInspection(True, program_ids=tuple(programs), account_keys=keys,
                        instructions=tuple(decoded),
                        num_signatures=num_signatures)


def inspect_bridge_transaction(raw_tx: Any, *, owner: str) -> TxInspection:
    """The Solana-origin bridge leg (CR-M04): :func:`inspect_transaction` with
    the pinned Relay program allowed, PLUS the pinned Relay program must be
    invoked and no top-level System value move may appear.

    Without these the Relay program was only an ALLOWANCE: a plain System
    ``Transfer`` of the declared amount to any address passed every check, and
    phase 2 then reported ``in_flight`` forever.
    """
    inspection = inspect_transaction(raw_tx, extra_allowed=RELAY_PROGRAM_IDS,
                                     owner=owner)
    if not inspection.ok:
        return inspection
    if not any(p in RELAY_PROGRAM_IDS for p in inspection.program_ids):
        return TxInspection(
            False,
            f"REFUSED: the bridge transaction never invokes the pinned Relay "
            f"program {sorted(RELAY_PROGRAM_IDS)} — it calls "
            f"{list(inspection.program_ids)}. Whatever it does, it is not a "
            f"Relay deposit. Nothing was signed.",
            program_ids=inspection.program_ids,
            account_keys=inspection.account_keys,
            instructions=inspection.instructions,
            num_signatures=inspection.num_signatures)
    refusal = system_value_move_refusal(inspection.instructions)
    if refusal:
        return TxInspection(
            False, f"REFUSED: {refusal}. Nothing was signed.",
            program_ids=inspection.program_ids,
            account_keys=inspection.account_keys,
            instructions=inspection.instructions,
            num_signatures=inspection.num_signatures)
    return inspection


# --------------------------------------------------------------------------
# The observation set + the simulate call
# --------------------------------------------------------------------------

# ⚠️ The two helpers below are the historical ONE-CALL view, CAPPED at
# `sim_max_addresses()`. `simulate()` no longer uses them (CR-H05): it builds
# the uncapped `observation_plan` and splits it across simulations.

def simulation_address_split(owner: str, *, mints: Sequence[str] = (),
                            rpc: Optional[Callable] = None,
                            account_keys: Sequence[str] = ()) -> tuple:
    """``(addresses, ours)`` — every account to fetch state for, and the subset
    that is OURS.

    ⚠️ These are two different questions and conflating them is a live money
    bug (found on prod 2026-09-11 by the first bridge dry run). `parse_deltas`
    sums native lamports across every pubkey it is told is ours; handed the FULL
    address list, a transfer from the owner INTO a third-party plain account in
    that list nets to roughly zero, and the SOL-drain assertion passes on a
    transaction that drained the wallet. Measured: owner −900,005,000, Relay
    vault +900,000,000, reported native_delta −5,000.

    Swaps never exposed it because an aggregator's route accounts are TOKEN
    accounts, which the native branch skips as parsed. A native-SOL destination
    is a plain account, so the bridge is the first path to reach it.
    """
    addresses = simulation_addresses(owner, mints=mints, rpc=rpc,
                                     account_keys=account_keys)
    ours: List[str] = [owner]
    for mint in mints:
        for ata in atas_for_mint(owner, mint, rpc):
            if ata not in ours:
                ours.append(ata)
    if rpc is not None:
        for account in owned_token_accounts(owner, rpc):
            if account not in ours:
                ours.append(account)
    # Only what actually survived the cap, and only what we actually fetched.
    kept = set(addresses)
    return addresses, [a for a in ours if a in kept]


def simulation_addresses(owner: str, *, mints: Sequence[str] = (),
                         rpc: Optional[Callable] = None,
                         account_keys: Sequence[str] = ()) -> List[str]:
    """The accounts `simulateTransaction` must report state for.

    Ordered by how much the assertion depends on them, because the list is
    capped: the owner and the DECLARED mints' associated accounts first (losing
    one of those disables an assertion outright), then every other token
    account the wallet owns, then the transaction's static keys.
    """
    addresses: List[str] = []

    def _add(value):
        if value and value not in addresses:
            addresses.append(value)

    _add(owner)
    for mint in mints:
        for ata in atas_for_mint(owner, mint, rpc):
            _add(ata)
    if rpc is not None:
        for account in owned_token_accounts(owner, rpc):
            _add(account)
    ours = len(addresses)
    for key in account_keys:
        _add(key)
    cap = sim_max_addresses()
    if len(addresses) > cap:
        if ours > cap:
            # Honest, and deliberately loud: the cut is now inside OUR OWN
            # accounts, so some of them genuinely go unobserved this run. Say
            # so rather than implying only third-party keys were dropped.
            logger.warning(
                "solana: this wallet owns %d token accounts, more than the "
                "%d-account simulation limit — %d of OUR OWN accounts are NOT "
                "observed by this simulation. The declared mints are still "
                "covered (they are ordered first); consolidate or close unused "
                "token accounts to restore full coverage.",
                ours, cap, ours - cap)
        else:
            logger.info(
                "solana: %d candidate accounts exceed the %d-account "
                "simulation limit — dropping the tail, which is third-party "
                "keys the delta parser discards anyway",
                len(addresses), cap)
        addresses = addresses[:cap]
    return addresses


def observation_plan(owner: str, *, mints: Sequence[str] = (),
                     rpc: Callable, created: Sequence[str] = ()) -> tuple:
    """``(addresses, ours)`` for the money path — UNCAPPED (CR-H05).

    ``addresses`` is every account the simulation must report: the owner, the
    declared mints' associated accounts, EVERY token account the wallet owns,
    and the accounts the transaction creates with our lamports. ``ours`` is the
    subset that is the wallet's (see :func:`simulation_address_split` for why
    the two must stay apart). Raises :class:`ObservationError` when the owned
    accounts cannot be enumerated or the set is larger than one pre-state read.
    """
    addresses: List[str] = []
    ours: List[str] = []

    def _add(value, mine):
        if value and value not in addresses:
            addresses.append(value)
        if mine and value and value not in ours:
            ours.append(value)

    _add(owner, True)
    for mint in mints:
        for ata in atas_for_mint(owner, mint, rpc):
            _add(ata, True)
    for account in owned_token_accounts(owner, rpc, strict=True):
        _add(account, True)
    for account in created:
        _add(account, False)
    if len(addresses) > MAX_OBSERVED_ACCOUNTS:
        raise ObservationError(
            f"the wallet needs {len(addresses)} accounts observed, more than "
            f"the {MAX_OBSERVED_ACCOUNTS} one pre-state read can return — "
            f"close unused token accounts")
    return addresses, ours


def simulate(raw_tx: Any, *, owner: str, mints: Sequence[str] = (),
             rpc: Callable, extra_allowed: frozenset = frozenset()) -> SolanaDeltas:
    """Vet, simulate and parse — the whole pre-broadcast observation.

    Fails CLOSED at every step: an undecodable or unrecognized transaction, an
    RPC error, or a simulation that did not run all return ``ok=False``, and
    the caller refuses on that.

    ⚠️ CR-H05: the observation is never TRUNCATED. The pinned RPC reports at
    most `sim_max_addresses()` accounts per `simulateTransaction`, so the
    must-observe set is split into as many simulations of the SAME bytes as it
    needs, and their post-states are joined in order. The earlier single call
    silently dropped every owned account past the fifth.
    """
    inspection = inspect_transaction(raw_tx, extra_allowed=extra_allowed,
                                     owner=owner)
    if not inspection.ok:
        return SolanaDeltas(False, inspection.reason)
    created = created_accounts(inspection.instructions, funder=owner,
                               strict=False)
    fee = fee_lamports(inspection.instructions, inspection.num_signatures)
    try:
        import base64
        addresses, ours = observation_plan(owner, mints=mints, rpc=rpc,
                                           created=created)
        # The PRE-state, in the SAME order, so parse_deltas has something to
        # compare against — `simulateTransaction` alone returns POST-state only.
        pre = (rpc("getMultipleAccounts",
                   [addresses, {"encoding": "jsonParsed"}])
               or {}).get("value") or []
        encoded = base64.b64encode(bytes(raw_tx)).decode()
        cap = sim_max_addresses()
        post: List[Any] = []
        units = None
        for start in range(0, len(addresses), cap):
            chunk = addresses[start:start + cap]
            sim = rpc("simulateTransaction", [
                encoded,
                {"encoding": "base64", "sigVerify": False,
                 "replaceRecentBlockhash": True,
                 "accounts": {"encoding": "jsonParsed", "addresses": chunk}}])
            value = sim.get("value") if isinstance(sim, dict) else None
            if not isinstance(value, dict):
                return parse_deltas(sim, owner=owner, owned_pubkeys=addresses,
                                    ours=ours)
            if value.get("err") is not None:
                return SolanaDeltas(False, f"simulation reverted: {value['err']}")
            accounts = list(value.get("accounts") or [])
            if len(accounts) != len(chunk):
                return SolanaDeltas(
                    False,
                    f"the simulation reported {len(accounts)} accounts for the "
                    f"{len(chunk)} requested — an account it did not report is "
                    f"an account it did not observe")
            post.extend(accounts)
            if units is None:
                units = value.get("unitsConsumed")
    except ObservationError as exc:
        return SolanaDeltas(False, f"the observation set is incomplete: {exc}")
    except Exception as exc:
        return SolanaDeltas(False, f"simulation failed: {exc}")
    sim = {"value": {"err": None, "accounts": post, "unitsConsumed": units},
           "_pre": pre}
    # `addresses` is what we FETCHED; `ours` is what is actually OURS. Passing
    # the former as `owned_pubkeys` made a third-party account's INFLOW cancel
    # our OUTFLOW inside `parse_deltas`'s native sum — see
    # `simulation_address_split`. Naming our own accounts is still what lets the
    # native branch run at all (without it native_delta comes back a
    # measured-looking 0 and the SOL-drain assertion never fires, prod
    # 2026-09-08); it just has to be OUR accounts, not every account.
    return parse_deltas(sim, owner=owner, owned_pubkeys=addresses, ours=ours,
                        created=created, fee_lamports=fee)
