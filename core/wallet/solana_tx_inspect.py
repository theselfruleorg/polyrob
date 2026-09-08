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

## The program allowlist

Defense in depth, not a replacement for the delta assertions: the threat model
is a malicious or buggy *aggregator response*, i.e. an attacker who controls the
transaction bytes but not a deployed program. A top-level instruction is where
such an attacker puts an injected `SetAuthority`/`CloseAccount`/`Approve`, and a
program id can never come from an address-lookup table (the runtime requires it
in the static keys), so enumerating the top-level program ids from the bytes is
COMPLETE for that layer. Anything unrecognized REFUSES.

What this does NOT cover, stated plainly: programs reached by CPI from an
allowed program (Jupiter routing into a DEX) are not enumerable from the
transaction, and are not checked here — the delta + authority assertions remain
the control for what those actually do.
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
BASE_ALLOWED_PROGRAMS = frozenset(
    {SYSTEM_PROGRAM_ID, COMPUTE_BUDGET_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID}
    | set(SPL_TOKEN_PROGRAMS) | set(JUPITER_PROGRAM_IDS) | set(MEMO_PROGRAM_IDS)
)

#: `getMultipleAccounts` refuses more than 100 keys, and the pre-state read uses
#: the SAME address list as the simulation so the two stay index-aligned. Our
#: own accounts are ordered FIRST, so a truncation can only ever drop a
#: third-party account — which `parse_deltas` discards anyway.
MAX_SIM_ADDRESSES = 100


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


def owned_token_accounts(owner: str, rpc: Callable) -> Tuple[str, ...]:
    """Every EXISTING token account the wallet owns, across both programs.

    This is what closes the "the simulation only watched the accounts the
    caller declared" hole: whatever route the transaction takes to reach one of
    these — a static key or an address-lookup table — naming it here means the
    simulation reports its state and `parse_deltas` can see a drain, a delegate
    grant or an authority change on it.
    """
    found: List[str] = []
    for program in sorted(SPL_TOKEN_PROGRAMS):
        try:
            res = rpc("getTokenAccountsByOwner",
                      [owner, {"programId": program}, {"encoding": "jsonParsed"}])
        except Exception as exc:
            logger.warning("solana: could not enumerate %s accounts for %s (%s) "
                           "— the observation set is narrower than intended",
                           program, owner, exc)
            continue
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


def inspect_transaction(raw_tx: Any) -> TxInspection:
    """Decode *raw_tx* and vet its TOP-LEVEL program ids against the allowlist."""
    try:
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(bytes(raw_tx))
        message = tx.message
        keys = tuple(str(k) for k in message.account_keys)
        programs: List[str] = []
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
    except Exception as exc:
        return TxInspection(
            False, f"the transaction bytes could not be decoded ({exc})")

    allowed = allowed_programs()
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
    return TxInspection(True, program_ids=tuple(programs), account_keys=keys)


# --------------------------------------------------------------------------
# The observation set + the simulate call
# --------------------------------------------------------------------------

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
    if len(addresses) > MAX_SIM_ADDRESSES:
        if ours > MAX_SIM_ADDRESSES:
            # Honest, and deliberately loud: the cut is now inside OUR OWN
            # accounts, so some of them genuinely go unobserved this run. Say
            # so rather than implying only third-party keys were dropped.
            logger.warning(
                "solana: this wallet owns %d token accounts, more than the "
                "%d-account simulation limit — %d of OUR OWN accounts are NOT "
                "observed by this simulation. The declared mints are still "
                "covered (they are ordered first); consolidate or close unused "
                "token accounts to restore full coverage.",
                ours, MAX_SIM_ADDRESSES, ours - MAX_SIM_ADDRESSES)
        else:
            logger.info(
                "solana: %d candidate accounts exceed the %d-account "
                "simulation limit — dropping the tail, which is third-party "
                "keys the delta parser discards anyway",
                len(addresses), MAX_SIM_ADDRESSES)
        addresses = addresses[:MAX_SIM_ADDRESSES]
    return addresses


def simulate(raw_tx: Any, *, owner: str, mints: Sequence[str] = (),
             rpc: Callable) -> SolanaDeltas:
    """Vet, simulate and parse — the whole pre-broadcast observation.

    Fails CLOSED at every step: an undecodable or unrecognized transaction, an
    RPC error, or a simulation that did not run all return ``ok=False``, and
    the caller refuses on that.
    """
    inspection = inspect_transaction(raw_tx)
    if not inspection.ok:
        return SolanaDeltas(False, inspection.reason)
    try:
        import base64
        addresses = simulation_addresses(
            owner, mints=mints, rpc=rpc, account_keys=inspection.account_keys)
        # The PRE-state, in the SAME order, so parse_deltas has something to
        # compare against — `simulateTransaction` alone returns POST-state only.
        pre = (rpc("getMultipleAccounts",
                   [addresses, {"encoding": "jsonParsed"}])
               or {}).get("value") or []
        sim = rpc("simulateTransaction", [
            base64.b64encode(bytes(raw_tx)).decode(),
            {"encoding": "base64", "sigVerify": False,
             "replaceRecentBlockhash": True,
             "accounts": {"encoding": "jsonParsed", "addresses": addresses}}])
    except Exception as exc:
        return SolanaDeltas(False, f"simulation failed: {exc}")
    if isinstance(sim, dict):
        sim = {**sim, "_pre": pre}
    return parse_deltas(sim, owner=owner)
