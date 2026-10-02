"""Operator release of a stuck submission-journal row (068 X2).

An unresolved journal row blocks EVERY later send — on-chain and x402 alike —
by design: a timeout cannot prove money did not move. Until now the only way out
was "operator_evidence_required" with no verb to act on the evidence, so one
rejected x402 payment could freeze the whole wallet.

A row is released ONLY by BOOKING it: a durable charge is appended to the same
audit ledger the rolling caps read (``<data>/wallet/audit.jsonl``), then the row
is marked booked. So the caps always count the worst case:

* an ``attempt:`` row (x402 / venue) books at least the amount it recorded;
* a transaction row needs ``charge_usd`` from the operator (the chain evidence
  names the fee, not the USD value that moved);
* ``never_sent`` books $0 and requires a written reason, which is recorded.

Refused while the evidence says the transaction may still land (no receipt,
included but not final, or an inconsistent block).
"""
from __future__ import annotations

import math
import time
from typing import Callable, Optional

#: Evidence outcomes that mean "it may still land or still change" — never release.
PENDING_OUTCOMES = frozenset({"unknown", "included_unfinalized", "reorg_or_inconsistent"})

MIN_REASON_CHARS = 8


class ReleaseRefused(ValueError):
    pass


#: The old journal cut idempotency keys to this many characters (068 R3-3).
LEGACY_TRUNCATED_KEY_CHARS = 512
#: The one bound on every lock a release takes (068 R3-4).
RELEASE_LOCK_TIMEOUT_SEC = 30.0


def _find(rows, reference: str) -> dict:
    from core.wallet.submission_journal import _identifier
    ref = _identifier(str(reference or "").strip())
    for row in rows:
        if row.get("tx_hash") == ref:
            return row
    raise ReleaseRefused(f"no unresolved submission {reference!r} — `polyrob wallet "
                         f"submissions` lists them")


def plan_release(row: dict, evidence: dict, *, charge_usd: Optional[float] = None,
                 never_sent: bool = False, reason: str = "") -> float:
    """The USD to book for *row*, or ``ReleaseRefused``. Pure."""
    reference = row["tx_hash"]
    outcome = str(evidence.get("outcome") or "unknown")
    is_chain_tx = not reference.startswith(("attempt:", "signing:"))
    if is_chain_tx and outcome in PENDING_OUTCOMES:
        raise ReleaseRefused(
            f"{reference} may still land ({outcome}: {evidence.get('detail')}). A release now "
            f"would let a second send race it. Wait for finality and re-run `polyrob wallet "
            f"submissions`.")
    if charge_usd is not None:
        if not math.isfinite(float(charge_usd)) or float(charge_usd) < 0:
            raise ReleaseRefused("--charge-usd must be a finite amount >= 0")
    if never_sent:
        if charge_usd:
            raise ReleaseRefused("--never-sent books $0; do not combine it with --charge-usd")
        if outcome == "finalized_success":
            raise ReleaseRefused(f"{reference} is FINAL and succeeded on chain — it was sent. "
                                 f"Book its value with --charge-usd instead.")
        if len((reason or "").strip()) < MIN_REASON_CHARS:
            raise ReleaseRefused("--never-sent needs --reason (at least "
                                 f"{MIN_REASON_CHARS} characters): why you know no money moved")
        return 0.0
    if reference.startswith("attempt:"):
        try:
            recorded = float(row.get("nonce"))
        except (TypeError, ValueError):
            recorded = None
        if recorded is None or not math.isfinite(recorded) or recorded < 0:
            raise ReleaseRefused(f"{reference} records no readable amount; pass --charge-usd")
        return max(recorded, float(charge_usd or 0.0))
    if charge_usd is None:
        raise ReleaseRefused(f"{reference} is a {'signing reservation' if reference.startswith('signing:') else 'transaction'}"
                             f" — the journal does not hold the USD it moved. Pass --charge-usd "
                             f"with the worst-case value, or --never-sent with --reason.")
    return float(charge_usd)


def booking_venue(row: dict) -> str:
    """The PolicyGate venue a released row books under (068 B5).

    A row written since 068 carries it (``venue``). A legacy row does not:
    an ``attempt:`` row stores its venue in ``chain`` (x402, hyperliquid,
    polymarket; ``signer:<chain>`` probes are on-chain → ``defi``), and every
    on-chain money writer in the tree books ``venue="defi"`` (swap, transfer,
    wrap, bridge, call, deploy, LP, launchpad, dapp, solana). Booking a legacy
    chain row as ``operator`` counted it globally but as $0 against the DeFi
    cap. The global cap counts every entry whatever its venue.
    """
    venue = str(row.get("venue") or "").strip().lower()
    if venue:
        return venue
    ref = str(row.get("tx_hash") or "")
    chain = str(row.get("chain") or "").strip().lower()
    if ref.startswith("attempt:") and chain and not chain.startswith("signer:"):
        return chain
    return "defi"


def _existing_charge(sink, ref: str) -> Optional[dict]:
    """The durable charge already booked for *ref*, by EITHER path, or None.

    068 B7: an earlier operator release (``submission_ref``), AND the normal
    booking — ``SpendLedger.record`` appends the charge with ``result_ref`` (the
    tx hash) or ``submission_ref`` (an ``attempt:`` row) and only THEN marks the
    row booked. A crash between those two leaves the row unresolved with its
    charge already in the ledger; releasing it must only mark it, never charge
    it a second time.
    """
    from core.wallet.submission_journal import _identifier
    for entry in reversed(list(sink)):
        for field in ("submission_ref", "result_ref"):
            value = entry.get(field)
            if isinstance(value, str) and value and _identifier(value.strip()) == ref:
                return entry
    return None


def _replay_key_refusal(row: dict, *, no_replay_key: bool, reason: str) -> Optional[str]:
    """068 N2: an x402 row journaled without its replay key (a legacy row).

    Its release cannot mark the original request as paid, so a retry of that
    request could pay a second time. The operator must say so explicitly."""
    key = row.get("idempotency_key") or ""
    # 068 R3-3: the journal once cut keys to 512 characters. A key of exactly
    # that length may be a truncated one the replay guard would never match, so
    # it is treated like a missing key. (Defensive: that column never reached a
    # deployed journal, but a local one may hold such a row.)
    possibly_truncated = len(key) == LEGACY_TRUNCATED_KEY_CHARS
    if (key and not possibly_truncated) or booking_venue(row) != "x402":
        return None
    if no_replay_key and len((reason or "").strip()) >= MIN_REASON_CHARS:
        return None
    what = ("a replay key that may have been CUT SHORT (exactly "
            f"{LEGACY_TRUNCATED_KEY_CHARS} characters, the old journal limit)"
            if possibly_truncated else "NO replay key (written before 068)")
    return (f"{row['tx_hash']} is an x402 payment journaled with {what}. Releasing "
            f"it cannot mark the original request as paid: a retry of that same "
            f"request COULD PAY AGAIN. Pass --no-replay-key with --reason (at least "
            f"{MIN_REASON_CHARS} characters) to release it anyway.")


def release_submission(reference: str, *, data_dir: Optional[str] = None,
                       charge_usd: Optional[float] = None, never_sent: bool = False,
                       reason: str = "", no_replay_key: bool = False,
                       inspect: Optional[Callable[[dict], object]] = None,
                       now: Optional[float] = None) -> dict:
    """Book and release one row. Returns the audit entry (written or found).

    068 B5–B7: the charge books under the row's ORIGINAL venue and replay key,
    and the whole read → charge → mark runs under the journal lock
    (``submission_journal.operator_release``), with at most one charge per
    submission reference — a retry after a crash finds the charge it already
    wrote instead of adding a second one.
    """
    from dataclasses import asdict, is_dataclass

    from core.wallet import submission_journal as sj
    from core.wallet.audit_sink import default_audit_sink

    row = _find(sj.unresolved(data_dir), reference)
    if inspect is None:
        from core.wallet.submission_recovery import inspect_submission as inspect
    evidence = inspect(row)
    evidence = asdict(evidence) if is_dataclass(evidence) else dict(evidence or {})
    amount = plan_release(row, evidence, charge_usd=charge_usd, never_sent=never_sent,
                          reason=reason)

    # 068 N3: ONE lock order for both paths — the audit reservation first, the
    # journal second, exactly as a normal spend holds them (reserve -> record ->
    # mark_booked). The charge is appended through THIS sink, under THIS
    # reservation.
    # 068 R3-4: ONE deadline for the whole lock acquisition. Building the sink
    # loads the ledger under the audit lock; with no bound, a stalled spender
    # held the release there before its own timeout had even started.
    import time as _time
    deadline = _time.monotonic() + RELEASE_LOCK_TIMEOUT_SEC
    try:
        sink = default_audit_sink(data_dir, lock_timeout=RELEASE_LOCK_TIMEOUT_SEC)
    except TimeoutError as exc:
        raise ReleaseRefused(str(exc)) from exc

    def _book(locked_row: dict) -> dict:
        ref = locked_row["tx_hash"]
        refused = _replay_key_refusal(locked_row, no_replay_key=no_replay_key,
                                      reason=reason)
        if refused:
            raise ReleaseRefused(refused)
        refresh = getattr(sink, "refresh", None)
        if refresh is not None:
            refresh()
        if getattr(sink, "healthy", True) is not True:
            raise ReleaseRefused("the wallet audit ledger is unhealthy — nothing was booked "
                                 "or released")
        prior = _existing_charge(sink, ref)
        if prior is not None:
            # Charged already (a crashed release, or the normal path crashed
            # between its charge and its mark): only the mark is missing.
            return {**prior, "submission_ref": ref, "already_booked": True}
        entry = {
            "ts": time.time() if now is None else float(now),
            "venue": booking_venue(locked_row),
            "action": "operator_release",
            "amount_usd": float(amount),
            "counterparty": None,
            # The ORIGINAL replay key when the row carries one, so the rebuilt
            # replay set refuses a retry of the same request (068 B6).
            "idempotency_key": (locked_row.get("idempotency_key")
                                or f"operator_release:{ref}"),
            "result_ref": None,
            "chain": None if ref.startswith("attempt:") else str(locked_row["chain"]),
            "asset": None,
            "submission_ref": ref,
            "evidence": evidence.get("outcome"),
            "never_sent": bool(never_sent),
            "reason": (reason or "").strip()[:500],
        }
        sink.append(entry)
        if getattr(sink, "healthy", True) is not True:
            raise ReleaseRefused("the audit charge could not be written durably — the row "
                                 "stays unresolved")
        return entry

    from contextlib import nullcontext
    reserve = getattr(sink, "reserve_blocking", None)
    try:
        remaining = max(0.0, deadline - _time.monotonic())
        with (reserve(timeout=remaining) if reserve is not None else nullcontext()):
            return sj.operator_release(row["tx_hash"], _book, data_dir=data_dir)
    except TimeoutError as exc:
        raise ReleaseRefused(str(exc)) from exc
    except ReleaseRefused:
        raise
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from exc
