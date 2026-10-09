"""`defi_trade.bridge` — the cross-chain move (proposal 037).

Kept out of `trade_tool.py` per the decomposition note: new behaviour gets its
own module rather than growing a file that is already 1.4k lines.

OWNER DECISION, 2026-09-12: **caps, not taps.** This supersedes the 2026-09-11
always-owner-approved rule, which this docstring described for a day after the
code stopped implementing it. The first cut asked for an owner tap on EVERY
bridge regardless of size, on the reasoning that a bridge is the only money verb
that can move the WHOLE treasury in one action. After actually using it the
owner's verdict was that a money rail needing a tap per move is not autonomy at
all — so the ceiling decides instead, and the properties that bound the damage
stay exactly where they were.

Concretely, this verb now tiers like `swap`/`solana_swap`:

* under `DEFI_AUTONOMOUS_MAX_USD` it runs and reports; over it, the durable
  owner queue. `defi_trade_bridge` is in `spend_lane.DEFI_SPEND_VERBS`.
* a delegated **sub-agent/leaf never bridges** — it reports back. That is not a
  size question and is not tiered (`_refuse_non_owner_turn`).
* the spend is RECORDED to the ledger, so every other money verb still counts it
  against their caps; an outflow Relay cannot value REFUSES rather than booking
  `$0.00`, which would silently widen every one of those caps.
* it still refuses on the 031 pause record, correspondent taint, and every
  phase-1 assertion in `core/wallet/bridge_guard.py`. ⚠️ There is no separate
  kill-switch to name — `AutonomyConfig.autonomy_halted()` is
  `not allows("dispatch").allowed`, a FACET of that same record.

Everything above is the *policy*. The *proof* is the two-phase guard: phase 1
asserts the send before broadcast, phase 2 asserts the ARRIVAL by measuring the
destination balance. See `core/wallet/bridge_guard.py`.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Default off. A money verb that ships armed is a money verb that ships wrong.
FLAG = "DEFI_BRIDGE_ENABLED"


def bridge_enabled() -> bool:
    from core.env import bool_env
    return bool_env(FLAG, False)


def _refuse_non_owner_turn(execution_context) -> Optional[str]:
    """Turn-origin gate, in line with the other money verbs.

    ⚠️ REWRITTEN on the owner's 2026-09-12 directive. The first cut refused the
    WHOLE autonomous-origin family outright — no lane, no exception — on the
    reasoning that a bridge can relocate the treasury in one action. Living with
    it showed the cost: the agent could never bridge for its owner, only tell him
    to do it himself, three taps at a time. That is not autonomy with a safety
    margin, it is a rail the owner operates by hand. The posture is now **caps,
    not taps** — the ONE shape: the kernel's leaf + principal steps
    (``core.money.authorize.authorize_spend``).
    """
    from core.money.authorize import SpendIntent, authorize_spend
    verdict = authorize_spend(SpendIntent(what="bridge", pause=False), execution_context)
    return verdict.reason if verdict.refused else None


def _solana_turn_refusal(tool, execution_context) -> tuple:
    """``(refusal | None, autonomous_origin)`` for a SOLANA-origin real run.

    CR-M02: `leaf_refusal` checks leaf/sub-agent/principal only, so a forged
    turn (self-wake, delegation-result) could bridge the Solana treasury and
    `DEFI_AUTONOMOUS_TURN_TRADING` was never consulted. The EVM origin gets
    this from `tx_guard.authorize`; the SVM origin has no EVM transaction to
    route through it, so it asks the ONE Solana mirror of tx_guard steps 1-2 —
    ``DefiTradeTool._solana_turn_gate`` — rather than a copy of it. A bridge is
    never exit-shaped. A tool without the gate refuses (fail closed).
    """
    gate_fn = getattr(tool, "_solana_turn_gate", None)
    if not callable(gate_fn):
        return ("refused: the Solana turn gate is unavailable on this tool; "
                "failing closed. Nothing was broadcast.", False)
    try:
        refusal, autonomous_origin, _exit = gate_fn(
            execution_context, exit_shaped_fn=lambda: False)
    except Exception as exc:
        return (f"refused: the turn gate failed ({exc}); failing closed.", False)
    return refusal, bool(autonomous_origin)


def _idempotency_key(*, origin_id, dest_id, dest_currency, amount_in_raw,
                     recipient, execution_context, now=None) -> str:
    """A replay key derived from the INTENT, not from a random suffix (CR-L02).

    ``uuid4()`` made every key unique, so the PolicyGate replay guard could
    never fire: a re-issued tool call re-quoted, got a new Relay request id and
    a new key, and sent again. The key is the move itself (origin, destination,
    asset, raw amount, recipient) inside one session and a 10-minute window —
    there is no turn id on the execution context to bind it tighter. A second
    identical bridge in a later window is a new decision and gets a new key.
    """
    window = int((now if now is not None else time.time()) // 600)
    session = str(getattr(execution_context, "session_id", "") or "")
    raw = "|".join(str(x) for x in (
        "bridge", origin_id, dest_id, str(dest_currency).lower(),
        int(amount_in_raw), recipient, session, window))
    return "defi_bridge:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _refuse_paused(execution_context=None) -> Optional[str]:
    """The 031 owner pause — the kernel's pause step.

    Live, 2026-09-12: the owner armed the trading rail, asked his agent in chat
    to bridge, and hit this branch. He was told it was a hard safety gate the
    agent could not self-grant, and advised to type `/bridge` himself — which
    would have hit this SAME check, because it runs before any seat distinction.
    Both halves of that answer were wrong, and the message is why; the sentence
    now lives beside the pause record (``core.autonomy_control.pause_refusal_text``).
    """
    from core.money.authorize import SpendIntent, authorize_spend
    # CR-L21: a bridge is never exit-shaped (see ``_solana_turn_refusal``), so
    # the scoped `/pause trading` binds it too.
    # 2026-09-26: a genuine owner turn (`/bridge … go`, or the owner asking in
    # chat) is the owner acting — the pause bounds only the agent's own work.
    from tools.controller.turn_origin import (
        _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)
    verdict = authorize_spend(SpendIntent(principal=False, leaf=False, entry=True),
                              execution_context, forged_fn=_is_forged_or_autonomous_turn,
                              autonomous_ok_fn=_is_autonomous_goal_turn)
    return verdict.reason if verdict.refused else None



async def _require_owner_approval(tool, *, params_summary: dict,
                                  execution_context,
                                  grant_key: Optional[dict] = None) -> tuple:
    """``(approved, note)``. Consulted ABOVE the autonomous ceiling only.

    ⚠️ This docstring used to read "ALWAYS consulted — there is no amount below
    which a bridge skips this", which was true of the first cut and false from
    `6d9bb790` ("the bridge runs on caps, not taps") onward: the only call site
    sits behind `amount_usd > _ceiling`.

    ⚠️ The wait is BOUNDED. ``OwnerQueueApprover.request`` polls until its
    caller's ``asyncio.wait_for`` cancels it — every other caller wraps it, and
    this one did not, so it polled forever. On 2026-09-25 the owner's
    ``/bridge … go`` ran on the Telegram poll loop, waited for a tap, and the tap
    could never be read: the whole bot froze until the ask was decided on the
    box. A timeout leaves the ask OPEN and the grant redeemable by the next run.
    """
    import asyncio
    try:
        from tools.controller.approval import approval_wait_timeout_sec
        from tools.controller.approval_queue import OwnerQueueApprover
        approver = OwnerQueueApprover(
            user_id=getattr(execution_context, "user_id", None))
        try:
            ok = await asyncio.wait_for(
                approver.request("defi_trade_bridge", params_summary,
                                 execution_context, hash_params=grant_key),
                timeout=approval_wait_timeout_sec("owner_queue"))
        except asyncio.TimeoutError:
            ok = False
        if ok:
            return True, "owner approved"
        return False, (
            "the owner has not granted this yet. The ask is DURABLE and waiting: "
            "the owner was sent the approval and decides it with one tap (it also "
            "shows in /pending).\n"
            "⚠️ Do NOT give the owner a command or an id to type. On 2026-09-12 "
            "an agent told its owner to run `/approve 0x1789…` (the relay request "
            "id), which matches nothing.\n"
            "Once granted, the next run of this SAME bridge redeems it "
            "automatically — do not re-quote in a loop while waiting.")
    except Exception as exc:
        # An approval path that ERRORS must deny. This is the one gate standing
        # between an agent turn and the whole treasury.
        logger.warning("bridge: approval path failed", exc_info=True)
        return False, f"approval could not be obtained ({exc}); failing closed."


async def _notify(tool, execution_context, notice, *, settled: bool) -> None:
    """Fire one owner notice. Swallows everything.

    A notification failure must never turn a settled transaction into an error,
    and must never leave a broadcast unrecorded — `tx_notify` already writes its
    audit event before it tries to deliver.
    """
    try:
        from core.wallet import tx_notify
        await tx_notify.notify(
            getattr(tool, "container", None),
            getattr(execution_context, "user_id", None), notice,
            settled=settled,
            session_id=getattr(execution_context, "session_id", None))
    except Exception:
        logger.debug("bridge: owner notice skipped (fail-open)", exc_info=True)


def _route(dest_name: Optional[str], params) -> str:
    """`base→robinhood`. One renderer so every notice about one bridge names it
    the same way."""
    return f"{(params.from_chain or '?').strip().lower()}→{dest_name or '?'}"


def _autonomous_ceiling(execution_context) -> float:
    """The per-transaction ceiling, read from the SAME place `tx_guard` reads it.

    One function so the dry run, the approval decision and the guard can never
    quote three different numbers at the owner.
    """
    from core.wallet import tx_guard
    return tx_guard.autonomous_max_usd(*tx_guard.ceiling_scope(execution_context))


def describe_quote(quote) -> str:
    """The human-readable order. Rendered identically on a dry run, a refusal
    and a success, so the owner compares like with like."""
    usd_in = ("unknown" if quote.amount_in_usd is None
              else f"${quote.amount_in_usd:,.2f}")
    usd_out = ("unknown" if quote.amount_out_usd is None
               else f"${quote.amount_out_usd:,.2f}")
    return (
        f"bridge {quote.amount_in_formatted:g} {quote.symbol_in} "
        f"(chain {quote.origin_chain_id}) -> {quote.symbol_out} "
        f"(chain {quote.dest_chain_id})\n"
        f"  value in  : {usd_in}\n"
        f"  quoted out: {quote.amount_out_formatted:.8f} {quote.symbol_out} "
        f"({usd_out})\n"
        f"  ARRIVAL FLOOR: {quote.min_out_formatted:.8f} {quote.symbol_out} "
        f"— phase 2 asserts the measured balance against this\n"
        f"  impact    : {quote.impact_pct}%   relay eta: "
        f"{quote.time_estimate_sec}s\n"
        f"  recipient : {quote.recipient}\n"
        f"  request id: {quote.request_id}\n")


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------

#: The ORIGIN is NATIVE-only, and that restriction is load-bearing: an ERC-20
#: origin adds an approve step whose spender is a third party's address, which
#: belongs in its own change with its own approval.
#:
#: The DESTINATION is not the same question. Receiving a token grants nobody
#: anything — the only thing it changes is which balance phase 2 must measure. So
#: an ERC-20 destination is supported, narrowly: only a token PINNED in the chain
#: registry (`wrapped_native` / `usdc`), never an arbitrary address. The addresses
#: in that table were each verified on-chain; a model-supplied address was not,
#: and "bridge my treasury to this address" is not a question to answer from a
#: string.
NATIVE = "native"


def resolve_dest_currency(token_out: str, chain_name: Optional[str]) -> tuple:
    """``(currency_address, label)`` or raise ValueError naming what is allowed.

    `native`/empty -> the native sentinel. Otherwise the token must resolve
    through the pinned registry row for the DESTINATION chain.
    """
    from tools.defi.providers.relay_bridge import NATIVE_EVM
    want = (token_out or NATIVE).strip().lower()
    if want in ("", NATIVE):
        return NATIVE_EVM, "native"

    from core.wallet import chains as _chains
    row = _chains.get(chain_name or "")
    if row is None:
        raise ValueError(f"chain {chain_name!r} is not in the pinned registry, so "
                         f"no token can be resolved on it")
    pinned = {}
    if row.wrapped_native:
        pinned["weth"] = pinned[f"w{(row.native_symbol or 'eth').lower()}"] = \
            row.wrapped_native
        pinned[str(row.wrapped_native).lower()] = row.wrapped_native
    if row.usdc:
        pinned["usdc"] = row.usdc
        pinned[str(row.usdc).lower()] = row.usdc

    got = pinned.get(want)
    if got is None:
        allowed = sorted({k for k in pinned if not k.startswith("0x")} | {NATIVE})
        raise ValueError(
            f"{token_out!r} is not a pinned asset on {chain_name}. A bridge may "
            f"only deliver an asset this wallet has verified on-chain — allowed "
            f"here: {', '.join(allowed)}. (Every address in the chain registry "
            f"was checked with eth_getCode plus symbol/decimals reads; a "
            f"model-supplied address was not.)")
    label = want if not want.startswith("0x") else got
    return got, label


def _resolve_endpoint(chain: str, tool) -> tuple:
    """``(chain_id, address, native_decimals, is_svm)`` for a registry chain name
    or ``"solana"``. Raises ValueError with an owner-readable reason."""
    from core.wallet import chains as _chains
    from tools.defi.providers.relay_bridge import SOLANA_CHAIN_ID

    wallet = tool._get_wallet()
    if wallet is None:
        raise ValueError("agent wallet not enabled (AGENT_WALLET_ENABLED)")

    name = (chain or "").strip().lower()
    if name == "solana":
        try:
            address = wallet.solana_address
        except Exception as exc:
            raise ValueError(f"no Solana address on this wallet: {exc}") from exc
        return SOLANA_CHAIN_ID, address, 9, True

    row = _chains.get(name)
    if row is None:
        # `solana` is a registry name too, so union rather than prepend —
        # listing it twice reads like two different chains.
        known = ", ".join(sorted(set(_chains.names()) | {"solana"}))
        raise ValueError(f"unknown chain {chain!r}. Known: {known}")
    chain_id = int(getattr(row, "chain_id", 0) or 0)
    if chain_id <= 0:
        raise ValueError(f"chain {name!r} has no EVM chain id in the registry")
    return chain_id, wallet.address, 18, False


async def perform_bridge(tool, params, execution_context=None):
    """Quote, assert, approve, broadcast, then PROVE THE ARRIVAL."""
    import asyncio as _asyncio  # CR-M10: the leg's RPC work runs off the loop
    from core.wallet import bridge_guard
    from tools.defi.providers.relay_bridge import (NATIVE_EVM, NATIVE_SVM,
                                                   RelayBridgeProvider, RelayError)

    if not bridge_enabled():
        return tool._ar(error=(
            f"bridging is off. Set {FLAG}=true to arm it. This is a money verb "
            f"that moves value between chains; it ships disarmed."))

    refusal = _refuse_non_owner_turn(execution_context)
    if refusal:
        return tool._ar(error=refusal)
    # ⚠️ The pause/kill-switch gate is checked for a REAL run only. A dry run
    # returns before anything is signed and provably broadcasts nothing, and
    # `spend_lane.py` states the rule this follows: "Blocking a quote buys no
    # safety at all, so the dry-run exemption here is UNCONDITIONAL."
    #
    # Found by the first prod dry run (2026-09-11): with the owner's stop in
    # force, `polyrob wallet bridge solana robinhood 0.9` refused to even QUOTE.
    # That is the "I stopped you, now I cannot see anything" failure — the owner
    # could not learn what a bridge would cost while deciding whether to resume.
    # The LEAF refusal above stays unconditional: a delegated sub-agent never
    # bridges, quote or not. An autonomous goal turn MAY bridge (caps, not
    # taps) — on the EVM origin `tx_guard.authorize` applies the forged-turn
    # and DEFI_AUTONOMOUS_TURN_TRADING bars, on the Solana origin the same
    # gate runs below (CR-M02).
    if not params.dry_run:
        refusal = _refuse_paused(execution_context)
        if refusal:
            return tool._ar(error=refusal)

    if (params.token_in or NATIVE).lower() != NATIVE:
        return tool._ar(error=(
            "the bridge ORIGIN must be the chain's native asset. An ERC-20 origin "
            "needs an allowance leg whose spender is a third party's address; "
            "that is its own change with its own approval. The DESTINATION may be "
            "a pinned token — receiving one grants nobody anything."))

    try:
        origin_id, sender, dec_in, svm_origin = _resolve_endpoint(params.from_chain, tool)
        dest_id, recipient, _dec_out, svm_dest = _resolve_endpoint(params.to_chain, tool)
    except ValueError as exc:
        return tool._ar(error=str(exc))

    svm_autonomous_origin = False
    if svm_origin and not params.dry_run:
        refusal, svm_autonomous_origin = _solana_turn_refusal(
            tool, execution_context)
        if refusal:
            return tool._ar(error=refusal)

    dest_name = bridge_guard.chain_name_for_id(dest_id)
    try:
        dest_currency, dest_label = resolve_dest_currency(
            params.token_out, dest_name)
    except ValueError as exc:
        return tool._ar(error=str(exc))

    if svm_dest:
        return tool._ar(error=(
            "bridging INTO Solana is not supported yet: phase 2 measures the "
            "arrival with an EVM `eth_getBalance` read, and asserting an SVM "
            "arrival needs its own reader. Refusing rather than trusting the "
            "provider's status field for the arrival."))

    wallet = tool._get_wallet()
    gate = getattr(wallet, "policy", None)
    if gate is None:
        return tool._ar(error=("refused: the wallet exposes no PolicyGate — a "
                               "money verb may not run ungoverned"))

    # ⚠️ Decimal, NOT float. `int(round(0.036 * 10**18))` is 35999999999999996 —
    # binary floating point cannot hold 0.036, so the amount we quote and sign is
    # not the amount the owner typed. It is four wei short here and harmless, but
    # the same expression is what phase 1 compares the quote against and what the
    # arrival floor is derived from, and "close enough" has no place in either.
    from decimal import Decimal
    amount_in_raw = int((Decimal(str(params.amount)) * (10 ** dec_in)).to_integral_value())
    if amount_in_raw <= 0:
        return tool._ar(error=f"amount must be positive, got {params.amount}")

    provider = RelayBridgeProvider()
    try:
        quote = await _asyncio.to_thread(provider.quote,
            origin_chain_id=origin_id, dest_chain_id=dest_id,
            origin_currency=NATIVE_SVM if svm_origin else NATIVE_EVM,
            dest_currency=dest_currency,
            amount_in_raw=amount_in_raw, sender=sender, recipient=recipient)
    except RelayError as exc:
        return tool._ar(error=f"no bridge route: {exc}")

    header = describe_quote(quote)
    if dest_label != "native":
        # `describe_quote` names the symbol; this names the ASSET we asked for,
        # which is the thing that decides how phase 2 measures the arrival.
        header += f"  delivering: {dest_label} ({dest_currency})\n"

    verdict = bridge_guard.assert_phase1(
        quote=quote, expected_recipient=recipient,
        declared_amount_raw=amount_in_raw, dest_chain_name=dest_name)
    if not verdict.ok:
        return tool._ar(content=header + f"  guard: {verdict.reason}\n"
                                         f"  RESULT: NOT SENT.")
    header += "  phase 1: assertions passed\n"
    from tools.defi.bridge_price import quote_refusal
    economic_refusal = await _asyncio.to_thread(
        quote_refusal, tool, quote, origin=params.from_chain.strip().lower(),
        destination=dest_name)
    if economic_refusal:
        header += f"  independent value check: {economic_refusal}\n"
        if not params.dry_run:
            return tool._ar(content=header + "  RESULT: NOT SENT.")
    else:
        header += "  independent value check: arrival floor and impact within bounds\n"

    # Build + simulate the ORIGIN leg. Exactly one of `raw_tx`/`prepared` is set
    # from here on — the two families sign through different rails and there is
    # deliberately no shared abstraction over "a signable thing", because the
    # assertions that make each one safe are not the same assertions.
    raw_tx = None
    signer = None
    prepared = None
    svm_usd = None
    idem = _idempotency_key(
        origin_id=origin_id, dest_id=dest_id, dest_currency=dest_currency,
        amount_in_raw=amount_in_raw, recipient=recipient,
        execution_context=execution_context)
    if svm_origin:
        from core.wallet.relay_svm import (RelaySvmBuildError, build_transaction,
                                           signer_accounts)
        required = signer_accounts(quote.tx_data.get("instructions") or [])
        if required != {sender}:
            return tool._ar(content=header + (
                f"  guard: REFUSED — the order requires signatures from "
                f"{sorted(required)}, not exactly this wallet ({sender}). "
                f"An order needing another signer is not our order.\n"
                f"  RESULT: NOT SENT."))
        try:
            signer = wallet.solana_signer()
        except Exception as exc:
            return tool._ar(error=f"no Solana signer: {exc}")
        from core.wallet.solana_rail import SolanaRail
        try:
            blockhash = await _asyncio.to_thread(SolanaRail(signer=None).recent_blockhash)
        except Exception as exc:
            return tool._ar(content=header + (
                f"  guard: REFUSED — no recent blockhash ({exc}); a transaction "
                f"built without one can never land.\n  RESULT: NOT SENT."))
        try:
            tx = build_transaction(
                instructions=quote.tx_data["instructions"],
                payer=sender, recent_blockhash=blockhash)
        except RelaySvmBuildError as exc:
            return tool._ar(content=header + f"  guard: {exc}\n  RESULT: NOT SENT.")
        raw_tx = bytes(tx)

        # CR-M04: the Relay program was only an ALLOWANCE — a payload that never
        # called it and moved the declared SOL with a plain System transfer
        # passed every check, and phase 2 then said `in_flight` forever. The
        # pinned program must be INVOKED, and no top-level System value move
        # may appear beside it.
        from core.wallet.solana_tx_inspect import (RELAY_PROGRAM_IDS,
                                                   inspect_bridge_transaction)
        shape = inspect_bridge_transaction(raw_tx, owner=sender)
        if not shape.ok:
            return tool._ar(content=header + (
                f"  guard: {shape.reason}\n  RESULT: NOT SENT."))

        # The bridge calls Relay's deposit program, which the default allowlist
        # deliberately excludes (a SWAP calling it is an anomaly). Widened HERE,
        # per-call, from a PINNED constant — never from the quote we are vetting.
        deltas = await _asyncio.to_thread(tool._solana_simulate, raw_tx=raw_tx,
                                        owner=sender, mints=(), extra_allowed=RELAY_PROGRAM_IDS)
        if deltas is None or not deltas.ok:
            reason = getattr(deltas, "reason", "no result") if deltas else "no result"
            return tool._ar(content=header + (
                f"  guard: REFUSED — the simulation did not pass ({reason}). A "
                f"simulation that did not run is not one that passed.\n"
                f"  RESULT: NOT SENT."))
        if deltas.grants_authority():
            return tool._ar(content=header + (
                f"  guard: REFUSED — this transaction changes AUTHORITY over "
                f"your accounts ({list(deltas.authority_grants)}). A bridge "
                f"grants nothing.\n  RESULT: NOT SENT."))
        if any(delta < 0 for delta in (deltas.token_deltas or {}).values()):
            return tool._ar(content=header + (
                "  guard: REFUSED — undeclared token outflow in a native bridge.\n"
                "  RESULT: NOT SENT."))
        outflow = -int(deltas.native_delta or 0)
        if outflow <= 0:
            return tool._ar(content=header + (
                f"  guard: REFUSED — the simulation shows no SOL leaving "
                f"(native delta {deltas.native_delta}). An outflow of nothing "
                f"is not a bridge.\n  RESULT: NOT SENT."))
        # Fee and rent are the SAME ASSET as the principal on a native move, so
        # the measured outflow is always amount + fee. Classify the excess the
        # way `solana_swap` does rather than refusing every real bridge.
        from core.wallet.solana_simulation import is_plausible_rent
        # A bridge that moves materially LESS than quoted is not the bridge that
        # was quoted. The over-move check below is the classic one, but only it
        # would have passed a transaction moving 5,000 lamports of a declared
        # 900,000,000 — which is exactly what a mis-measured simulation looked
        # like on prod before the owned-account fix. The floor is 99%: routing
        # dust and a fee are expected, a shortfall of orders of magnitude is not.
        if outflow < amount_in_raw - max(1, amount_in_raw // 100):
            return tool._ar(content=header + (
                f"  guard: REFUSED — the simulation moves only {outflow} "
                f"lamports of the declared {amount_in_raw}. A bridge that sends "
                f"materially less than it quoted is not the order that was "
                f"priced, and the arrival floor was computed from the quoted "
                f"amount.\n  RESULT: NOT SENT."))
        excess = outflow - amount_in_raw
        if excess > 0 and not is_plausible_rent(-excess):
            return tool._ar(content=header + (
                f"  guard: REFUSED — {outflow} lamports leave but only "
                f"{amount_in_raw} was declared, and the {excess} difference is "
                f"far more than fees and rent explain.\n  RESULT: NOT SENT."))
        header += (f"  simulated: native delta {deltas.native_delta} lamports "
                   f"(declared {amount_in_raw})\n")
        # CR-M03 + CR-L06: the value is the MEASURED lamport outflow — principal,
        # fee and any excess — at the pinned wSOL price, independent of Relay's
        # `amountUsd` (a figure from the same party whose order is being
        # vetted). Relay's figure can only RAISE it. Unpriced refuses: no cap
        # can bound a number we do not have.
        from tools.defi.spl_deploy_verb import _price_sol
        svm_usd = await _asyncio.to_thread(_price_sol, tool, outflow / 1_000_000_000)
        if svm_usd is None:
            return tool._ar(content=header + (
                "  guard: REFUSED — the SOL leaving could not be priced at the "
                "pinned wSOL price, so no cap can bound it (Relay's own USD "
                "figure is not an independent valuation).\n  RESULT: NOT SENT."))
        if quote.amount_in_usd is not None:
            svm_usd = max(svm_usd, round(float(quote.amount_in_usd), 2))
        header += f"  valued:    ${svm_usd:,.2f} (measured outflow, pinned price)\n"
    else:
        # EVM ORIGIN (039 B2). The twin of the branch above, in its own module.
        # What was missing was never the broadcast rail — it has moved value
        # since 023 — but a way to DECLARE a native-value send to `tx_guard`,
        # which was ERC-20-only until 039 B1.
        from tools.defi.bridge_evm_leg import EvmOriginLeg
        if quote.amount_in_usd is None:
            # Checked HERE rather than at the shared site below, because the
            # guard's own cap check is keyed on this number: passing it through
            # as 0.0 would refuse with "exceeds the declared max_spend_usd
            # $0.0000", which is true and tells the owner nothing.
            return tool._ar(content=header + (
                "  guard: REFUSED — Relay returned no USD value for the outflow, "
                "so the guard has no figure to hold this transaction to and the "
                "spend could not be booked honestly.\n  RESULT: NOT SENT."))
        try:
            evm_signer = wallet.operational_signer()
        except Exception as exc:
            return tool._ar(error=f"no EVM signer: {exc}")
        evm_leg = EvmOriginLeg(chain=(params.from_chain or "").strip().lower(),
                               signer=evm_signer)
        prepared = await _asyncio.to_thread(
            evm_leg.prepare,
            tx_data=quote.tx_data, origin_chain_id=origin_id,
            amount_in_raw=amount_in_raw, amount_usd=quote.amount_in_usd,
            gate=gate, execution_context=execution_context, tool=tool,
            idempotency_key=idem)
        header += prepared.header
        if not prepared.ok:
            # `prepared.header` ALREADY carries the guard verdict — appending
            # `prepared.reason` here printed the refusal twice, which reads like
            # two different problems. The header is the one copy.
            return tool._ar(content=header + "  RESULT: NOT SENT.")

    amount_usd = svm_usd if svm_origin else quote.amount_in_usd
    if amount_usd is None and not params.dry_run:
        # The ledger is the only place every OTHER money verb sees this spend
        # against ITS cap. Booking an unknown as 0.00 would silently widen every
        # other verb's remaining headroom by the size of this bridge — the same
        # confident-zero class as the 114 tenantless wallet_spend rows (033).
        return tool._ar(content=header + (
            "  guard: REFUSED — Relay returned no USD value for the outflow, so "
            "this spend cannot be booked honestly. Recording it as $0.00 would "
            "widen every other money verb's remaining cap by the size of this "
            "bridge.\n  RESULT: NOT SENT."))

    if params.dry_run:
        # ⚠️ The old text here read "approval: WOULD BE REQUESTED (a bridge is
        # always owner-approved)". That stopped being true on 2026-09-12 when the
        # owner replaced taps with caps, and a dry run that misreports the lane
        # teaches the owner to expect a tap that never comes — or worse, not to
        # expect one that does.
        _dry_ceiling = _autonomous_ceiling(execution_context)
        _over = amount_usd is not None and amount_usd > _dry_ceiling
        return tool._ar(content=header + (
            f"  lane:   {'owner queue — above' if _over else 'autonomous — within'} "
            f"the ${_dry_ceiling:,.2f} ceiling\n"
            "  phase 2 : would assert the measured arrival against the floor\n"
            "\n[DRY RUN] nothing was broadcast."))

    # RPC trust, the `solana_swap` mirror: the blockhash, the simulation and the
    # balance reads all come from the RPC, so a shared public endpoint cannot be
    # the trust anchor for moving funds across a chain boundary. Dry runs above
    # are a $0 read and stay available unpinned.
    import os as _os
    if svm_origin and not _os.getenv("DEFI_SOLANA_RPC", "").strip():
        return tool._ar(content=header + (
            "  guard: REFUSED — no pinned RPC for solana. The blockhash, the "
            "simulation and the caps all read from it, so the shared public "
            "endpoint cannot authorize moving funds — set DEFI_SOLANA_RPC. "
            "Dry runs are unaffected.\n  RESULT: NOT SENT."))

    # CR-L12: phase 2 proves an arrival by a balance RISE, which cannot say
    # whose rise it is. Two open bridges into the same (chain, recipient,
    # asset) would each claim the other's funds, so a second one waits.
    try:
        _open = bridge_guard.conflicting_open_bridge(
            dest_chain_id=dest_id, recipient=recipient,
            currency_out=quote.currency_out)
    except Exception as exc:
        return tool._ar(content=header + (
            f"  guard: REFUSED — the bridge store could not be read ({exc}), so "
            f"an open bridge into the same balance cannot be ruled out.\n"
            f"  RESULT: NOT SENT."))
    if _open is not None:
        return tool._ar(content=header + (
            f"  guard: REFUSED — bridge {_open.get('id')} into the same "
            f"{dest_name} balance is still {_open.get('state')}. Its arrival is "
            f"proven by a balance rise, and a second bridge into the same "
            f"balance would make either arrival unprovable. Wait for it to "
            f"settle: the bridge watcher re-measures the destination and tells "
            f"the owner when it lands or is refunded.\n  RESULT: NOT SENT."))

    # Measure the destination BEFORE. An unreadable balance here is fatal: phase
    # 2 has nothing to compare against, and a bridge whose arrival cannot be
    # proven is one we do not start.
    # The reader is chosen by WHAT IS ARRIVING, from the quote's own
    # `currency_out`. Measuring the native balance for an ERC-20 arrival would
    # watch a number that cannot move and report a real delivery as `in_flight`
    # forever.
    measure = bridge_guard.arrival_reader(quote.currency_out)
    balance_before = await _asyncio.to_thread(measure, recipient, dest_name)
    if balance_before is None:
        return tool._ar(content=header + (
            f"  guard: REFUSED — could not read {recipient} on {dest_name} "
            f"before sending. Without a baseline the arrival cannot be proven, "
            f"and an unprovable bridge is one that starts unprovable.\n"
            f"  RESULT: NOT SENT."))

    # CAPS, NOT TAPS (owner directive 2026-09-12). The first cut asked for an
    # owner tap on EVERY bridge regardless of size; the owner's own verdict after
    # using it was that a money rail needing three taps from him is not autonomy.
    # So the ceiling decides, exactly as it does for swap/solana_swap: under it
    # the bridge runs and reports; over it, the durable owner queue.
    _ceiling = _autonomous_ceiling(execution_context)
    # ONE decision, two sources. On the EVM path `tx_guard` already applied this
    # ceiling and answered `lane="owner_queue"`, which means "ask, then proceed" —
    # re-deriving it here from the quote would be a second opinion on the same
    # question, and two ceilings that can disagree is how a gate goes quiet. The
    # SVM path has no `authorize` to ask, so it compares directly.
    # A genuine owner turn (`/bridge … go`) IS the owner asking — on the EVM
    # path tx_guard step 0 already said so; the SVM path asks the same predicate.
    from core.money.authority import owner_direct_turn
    from tools.controller.turn_origin import (
        _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)
    _needs_owner = (prepared.needs_owner_approval if prepared is not None
                    else (amount_usd > _ceiling and not owner_direct_turn(
                        execution_context, _is_forged_or_autonomous_turn, tool)))
    if _needs_owner:
        approved, note = await _require_owner_approval(
            tool,
            params_summary={"from_chain": params.from_chain, "to_chain": params.to_chain,
                            "amount": float(params.amount), "symbol": quote.symbol_in,
                            "usd": amount_usd, "recipient": recipient,
                            "min_out": quote.min_out_formatted,
                            "request_id": quote.request_id},
            # ⚠️ The grant is keyed on the STABLE intent, NOT on the figures
            # above. A bridge re-quotes before every attempt, so `usd`,
            # `min_out` and `request_id` move each time — keying on them minted a
            # fresh tap per attempt (three for one bridge on 2026-09-12) and made
            # an approval unredeemable by the very next run. What the owner is
            # approving is "move THIS much of THIS asset from here to there, to
            # MY address"; the price is re-asserted at execution by the arrival
            # floor, the simulation and every cap.
            grant_key={"from_chain": params.from_chain, "to_chain": params.to_chain,
                       "amount": float(params.amount), "token_out": dest_label,
                       "recipient": recipient},
            execution_context=execution_context)
        if not approved:
            return tool._ar(content=header + (
                f"  approval: ${amount_usd:,.2f} is above the autonomous ceiling "
                f"${_ceiling:,.2f} — {note}\n  RESULT: NOT SENT."))
        header += (f"  approval: granted by the owner (${amount_usd:,.2f} over the "
                   f"${_ceiling:,.2f} ceiling)\n")
        if prepared is not None:
            # CR-L03: the owner ask polls, so the leg prepared above may be minutes
            # old — nonce, fees and chain state simulated at `latest` before the
            # wait. Build and authorize it again so what is broadcast is what the
            # guard just simulated.
            prepared = await _asyncio.to_thread(
                evm_leg.prepare,
                tx_data=quote.tx_data, origin_chain_id=origin_id,
                amount_in_raw=amount_in_raw, amount_usd=quote.amount_in_usd,
                gate=gate, execution_context=execution_context, tool=tool,
                idempotency_key=idem)
            if not prepared.ok:
                return tool._ar(content=header + "  re-check after approval:\n"
                                + prepared.header + "  RESULT: NOT SENT.")
    else:
        if svm_autonomous_origin and not getattr(gate, "has_daily_cap", False):
            # The `solana_swap` bar, for the same reason: an unattended origin
            # needs an aggregate damage bound, not only a per-tx ceiling.
            return tool._ar(content=header + (
                "  guard: REFUSED — an unattended bridge needs an aggregate "
                "damage bound; set WALLET_DAILY_CAP_USD.\n  RESULT: NOT SENT."))
        header += (f"  lane:  autonomous — ${amount_usd:,.2f} is within the "
                   f"${_ceiling:,.2f} ceiling\n")

    async with gate.reserve():
        # Re-check after acquiring the same reservation used by every sender.
        try:
            if bridge_guard.conflicting_open_bridge(
                    dest_chain_id=dest_id, recipient=recipient,
                    currency_out=quote.currency_out) is not None:
                return tool._ar(error="Another bridge into this balance is still pending; nothing was sent")
        except Exception:
            return tool._ar(error="Cannot verify pending bridges; nothing was sent")
        bid = bridge_guard.record_pending(
            user_id=str(getattr(execution_context, "user_id", "") or "owner"),
            quote=quote, amount_usd=amount_usd, balance_before=balance_before)
        # M10 (security analysis 2026-09-23): the PolicyGate check INSIDE the
        # reserve, on BOTH origins. The SVM path used to record without ever
        # checking (no kill-switch, per-tx ceiling, daily cap, replay or
        # submission-journal check), and the EVM check in tx_guard ran outside
        # this lock, so two concurrent bridges could both clear a nearly
        # exhausted cap. check -> send -> record is now one critical section.
        from contextlib import nullcontext
        from core.money.ledger import pause_probe
        owner_direct = owner_direct_turn(execution_context, _is_forged_or_autonomous_turn, tool)
        with pause_probe(lambda: False) if owner_direct else nullcontext():
            verdict = gate.check(venue="defi", amount_usd=float(amount_usd or 0.0),
                                 idempotency_key=idem)
        if not verdict.allowed:
            bridge_guard.settle(bid, state=bridge_guard.STATE_FAILED,
                                detail=f"refused by PolicyGate: {verdict.reason}")
            return tool._ar(content=header + (
                f"  guard: refused by PolicyGate: {verdict.reason}\n"
                f"  RESULT: NOT SENT — nothing was broadcast."))
        if prepared is not None:
            from tools.defi.bridge_evm_leg import EvmOriginLeg
            _sent = await _asyncio.to_thread(EvmOriginLeg.send, prepared)
            if _sent.state == "unknown":
                # It may have left: parked in flight, so the arrival watcher
                # measures the destination — never FAILED, never "not sent".
                bridge_guard.settle(bid, state=bridge_guard.STATE_IN_FLIGHT,
                                    detail=_sent.detail)
                return tool._ar(error=_sent.detail)
            if _sent.state == "error":
                bridge_guard.settle(bid, state=bridge_guard.STATE_FAILED,
                                    detail=_sent.detail)
                return tool._ar(error=_sent.detail)
            signature = _sent.tx_hash
        else:
            # CR-M05 parity with `solana_swap`: Relay chose the blockhash. Sign
            # only a transaction whose blockhash the pinned RPC says is still
            # valid; a tool without the check refuses (fail closed).
            _bh = getattr(tool, "_solana_blockhash_valid", None)
            try:
                bh_ok, bh_detail = ((False, "no blockhash check on this tool")
                                    if not callable(_bh) else _bh(raw_tx))
            except Exception as exc:
                bh_ok, bh_detail = False, f"check failed: {exc}"
            if not bh_ok:
                bridge_guard.settle(bid, state=bridge_guard.STATE_FAILED,
                                    detail=f"blockhash not valid: {bh_detail}")
                return tool._ar(content=header + (
                    f"  guard: REFUSED — the transaction's recent blockhash is "
                    f"not valid on the pinned RPC ({bh_detail}).\n"
                    f"  RESULT: NOT SENT — nothing was signed or broadcast; "
                    f"re-quote and try again."))
            try:
                signature = await _asyncio.to_thread(tool._solana_send, raw_tx, signer)
            except Exception as exc:
                from core.wallet.broadcast.evm import (
                    broadcast_failure_text, outcome_unknown)
                _unknown = outcome_unknown(exc)
                bridge_guard.settle(bid, state=(bridge_guard.STATE_IN_FLIGHT if _unknown
                                                else bridge_guard.STATE_FAILED),
                                    detail=broadcast_failure_text(exc))
                return tool._ar(error=broadcast_failure_text(exc),
                    error_kind=broadcast_error_kind(exc))
        # Checked above (M10) and recorded here, so every OTHER money verb
        # counts this spend against its caps.
        try:
            gate.record(venue="defi", action="bridge",
                        amount_usd=float(amount_usd or 0.0), counterparty=recipient,
                        idempotency_key=idem, result_ref=signature,
                        chain=params.from_chain)
        except Exception:
            logger.warning("bridge %s: ledger record failed", bid, exc_info=True)

    header += f"  BROADCAST: {signature}\n"

    # The owner learns the funds left BEFORE anything is known about where they
    # landed. That window — broadcast to arrival — is the one the 2026-09-12
    # session had no visibility into at all.
    from core.wallet import tx_notify
    _used, _limit = tx_notify.caps_from_gate(gate)
    # ⚠️ EVERY bridge notice carries the ORIGIN chain, not the destination —
    # including the arrival one. `tx_ref` is the origin transaction throughout
    # (`bridge_guard.settle(tx_ref=signature)` records the SEND), so linking it
    # to the destination would put a Solana base58 signature inside an EVM
    # explorer URL: a link that looks authoritative and resolves to nothing.
    # The discipline is `tx_notify`'s own — a link only for the chain the
    # transaction LANDED on. The ARRIVAL has no hash of its own to link; it is
    # proven by a measured balance, which the notice states as `measured`.
    _origin_chain = (params.from_chain or "").strip().lower() or None
    _sent_notice = tx_notify.TxNotice(
        verb="bridge", route=_route(dest_name, params), chain=_origin_chain,
        amount_in=f"{quote.amount_in_formatted:g} {quote.symbol_in}",
        amount_out=f"≥{quote.min_out_formatted:.8f} {quote.symbol_out}",
        usd=amount_usd, tx_ref=signature,
        extra_refs=(quote.request_id, bid),
        lane=("owner-approved" if _needs_owner else "autonomous"),
        cap_used_usd=_used, cap_limit_usd=_limit)
    await _notify(tool, execution_context, _sent_notice, settled=False)

    # Confirm the ORIGIN leg landed before waiting on an arrival. Without this a
    # send whose blockhash expired — so nothing left the wallet at all — polls
    # the destination for the full deadline and then reports `in_flight`, which
    # says "your money is somewhere between two chains" about a transaction that
    # never existed. That is the worst available answer: it is frightening AND
    # wrong, and it is the one that tempts a re-send. A reverted origin tx is
    # likewise terminal (the fee was paid, the value did not move).
    import asyncio as _asyncio

    if prepared is not None:
        from tools.defi.bridge_evm_leg import EvmOriginLeg
        _out = await _asyncio.to_thread(EvmOriginLeg.confirm, prepared, signature)
        # Same three answers as the Solana reader, deliberately: a receipt with
        # status 0 is `reverted` and terminal, and NO receipt is an open question
        # — never a failure, because on a bridge a blind re-send pays twice.
        origin = {"confirmed": "confirmed", "reverted": "reverted"}.get(
            _out.state, "unknown")
        detail = _out.detail
    else:
        from core.wallet.solana_rail import confirmation_outcome
        try:
            confirmed, detail = await _asyncio.to_thread(tool._solana_confirm, signature)
        except Exception as exc:
            confirmed, detail = False, f"status read failed: {exc}"
        origin = confirmation_outcome(confirmed, detail)
    if origin == "reverted":
        bridge_guard.settle(bid, state=bridge_guard.STATE_FAILED,
                            detail=f"origin transaction REVERTED: {detail}",
                            tx_ref=signature)
        await _notify(tool, execution_context, tx_notify.TxNotice(
            verb="bridge", route=_sent_notice.route, chain=_origin_chain,
            tx_ref=signature,
            extra_refs=(bid,), state=tx_notify.STATE_REVERTED,
            detail=f"origin transaction reverted: {detail}",
            ledger_recorded=True), settled=True)
        return tool._ar(content=header + (
            f"  origin  : REVERTED ON-CHAIN — nothing left the wallet, but the "
            f"fee was spent. {detail}\n"
            f"  RESULT: not bridged. bridge id {bid}"))
    if origin != "confirmed":
        bridge_guard.settle(bid, state=bridge_guard.STATE_IN_FLIGHT,
                            detail=f"origin transaction NOT confirmed: {detail}",
                            tx_ref=signature)
        await _notify(tool, execution_context, tx_notify.TxNotice(
            verb="bridge", route=_sent_notice.route, chain=_origin_chain,
            tx_ref=signature,
            extra_refs=(bid,), state=tx_notify.STATE_IN_FLIGHT,
            amount_out=_sent_notice.amount_out,
            detail=f"origin not confirmed: {detail}",
            ledger_recorded=True), settled=True)
        _why = ("On Solana this is far more often an expired blockhash than a "
                "pending transaction, so the send may never have happened."
                if prepared is None else
                "On an EVM chain the transaction is in the mempool and may still "
                "land, so the hash is the thing to check.")
        return tool._ar(content=header + (
            f"  origin  : NOT CONFIRMED — {detail}. {_why} Do NOT re-send "
            f"blindly: check {signature} first.\n"
            f"  RESULT: unproven. bridge id {bid}"))
    header += "  origin  : confirmed on-chain\n"

    # Phase 2, off the event loop: it polls with blocking sleeps and a stalled
    # loop would freeze every other session's turn.
    outcome = await _asyncio.to_thread(
        bridge_guard.await_arrival,
        provider=provider, request_id=quote.request_id, recipient=recipient,
        chain_name=dest_name, min_out_raw=quote.min_out_raw,
        balance_before=balance_before, read_balance=measure)

    bridge_guard.settle(bid, state=outcome.state, detail=outcome.detail,
                        tx_ref=signature, balance_after=outcome.balance_after)

    _settled_state = {bridge_guard.STATE_ARRIVED: tx_notify.STATE_ARRIVED,
                      bridge_guard.STATE_FAILED: tx_notify.STATE_FAILED}.get(
        outcome.state, tx_notify.STATE_IN_FLIGHT)
    _measured = None
    if outcome.measured_delta is not None:
        _measured = (f"+{(outcome.measured_delta) / (10 ** quote.decimals_out):.8f} "
                     f"{quote.symbol_out} on {dest_name}")
    await _notify(tool, execution_context, tx_notify.TxNotice(
        verb="bridge", route=_sent_notice.route, chain=_origin_chain,
        tx_ref=signature,
        extra_refs=(bid,), state=_settled_state, measured=_measured,
        amount_out=_sent_notice.amount_out, usd=amount_usd,
        detail=outcome.detail, ledger_recorded=True), settled=True)

    if outcome.state == bridge_guard.STATE_ARRIVED:
        arrived = (outcome.measured_delta or 0) / (10 ** quote.decimals_out)
        return tool._ar(content=header + (
            f"  phase 2 : ARRIVED — measured +{arrived:.8f} {quote.symbol_out} "
            f"on {dest_name} (floor {quote.min_out_formatted:.8f})\n"
            f"  RESULT: bridged. bridge id {bid}"))
    if outcome.state == bridge_guard.STATE_FAILED:
        return tool._ar(content=header + (
            f"  phase 2 : FAILED — {outcome.detail}\n"
            f"  RESULT: the order did not deliver. bridge id {bid}"))
    return tool._ar(content=header + (
        f"  phase 2 : IN FLIGHT — {outcome.detail}\n"
        f"  RESULT: sent, arrival NOT yet proven. bridge id {bid}"))
