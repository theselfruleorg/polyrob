"""The EVM-origin half of `defi_trade.bridge` (039 Unit B2).

`bridge_verb.py` carried a bare refusal here — "an EVM-origin bridge is not wired
yet" — and that refusal was honest about the symptom but not about the cause. The
quote, phase 1 and phase 2 of the two-phase guard are all chain-agnostic; Relay
returns ordinary EVM calldata `{to, data, value, chainId}` for an EVM origin. What
was missing was not a broadcast rail (`core/wallet/broadcast/evm.py` has moved
value since 023) but a way to DECLARE a native-value send to `tx_guard`, which was
ERC-20-only by construction until 039 B1.

This module is the EVM twin of the `svm_origin` branch, and it is deliberately a
separate file: `bridge_verb.py` is already the longest money verb, and the
decomposition note in AGENTS.md says new behaviour gets its own module rather than
growing one that is already large.

Trust boundary, unchanged from the rest of the rail: Relay decides the PATH. It
does not decide what we sign. The order it returns is asserted against the order
we priced BEFORE anything is built, `tx_guard` then simulates the built
transaction and asserts the measured native outflow against the declared amount,
and only then is it signed.

⚠️ A `pending` receipt is NOT a failure. The origin transaction may still land,
and a re-sent bridge pays twice — so a timeout returns `pending` carrying the
hash, and the caller parks the row `in_flight` rather than retrying.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


#: Headroom on the declared ceiling, to absorb PRICE-SOURCE DISAGREEMENT.
#:
#: `max_spend_usd` was the provider's own `amountUsd`, while `tx_guard` prices the
#: outflow ITSELF through the chain's pinned wrapped-native feed — deliberately,
#: because a provider must never be the authority on what our money is worth. Two
#: independent oracles never agree to the cent, so demanding
#: `ours <= theirs` made the verdict a coin flip on which feed was momentarily
#: higher. Measured live on prod 2026-09-12: the same 0.036 ETH bridge refused at
#: 15:16 (ours ~0.027% above Relay's) and passed at 15:20 ($91.38 vs $91.42).
#: "Bridge guard self-refuses at any amount" was the agent's own summary.
#:
#: ⚠️ This is NOT extra spending authority. It widens only the caller's DECLARED
#: figure, which exists to catch a transaction moving MATERIALLY more than quoted.
#: Everything that bounds real loss still runs against the guard's own valuation:
#: the per-transaction ceiling, the rolling 24h cap, the owner-queue threshold,
#: and the arrival floor phase 2 measures. A sub-1% oracle difference is not a
#: transaction moving more than it said.
VALUATION_TOLERANCE = 0.01


@dataclass(frozen=True)
class PreparedLeg:
    """A built, simulated, gas-sized transaction — or a refusal.

    ``needs_owner_approval`` is NOT a refusal. ``tx_guard`` returns
    ``lane="owner_queue"`` for a value above the autonomous ceiling, which means
    "ask, then proceed", and the caller owns that conversation. Collapsing it into
    ``ok=False`` would turn every above-ceiling bridge into a dead end.
    """
    ok: bool
    reason: str
    rail: Any = None
    tx: Optional[dict] = None
    amount_usd: Optional[float] = None
    needs_owner_approval: bool = False
    header: str = ""


@dataclass(frozen=True)
class SentLeg:
    state: str                    # "confirmed" | "reverted" | "pending" | "error"
    detail: str
    tx_hash: Optional[str] = None


def assert_order_matches_request(tx_data: dict, *, origin_chain_id: int,
                                 amount_in_raw: int) -> Optional[str]:
    """The wallet's own half of phase 1 for an EVM origin, or None if it holds.

    `RelayBridgeProvider._parse_quote` already asserted the quote against what we
    asked for. This asserts the SIGNABLE ITEM against the same request, which is a
    different object: a quote whose `details` describe our order but whose
    transaction carries someone else's `value` is exactly the shape this catches.
    """
    to = str(tx_data.get("to") or "").strip()
    if not to.startswith("0x") or len(to) != 42:
        return (f"REFUSED — the deposit item names no usable EVM destination "
                f"({to!r}). There is nothing safe to sign here.")

    try:
        chain_id = int(str(tx_data.get("chainId")))
    except (TypeError, ValueError):
        return ("REFUSED — the deposit item states no chain id. The same EOA "
                "exists on every EVM chain, so an unstated chain is a "
                "transaction that could land somewhere we did not choose.")
    if chain_id != int(origin_chain_id):
        return (f"REFUSED — the deposit item targets chain {chain_id}, not the "
                f"origin {origin_chain_id}.")

    try:
        value = int(str(tx_data.get("value")))
    except (TypeError, ValueError):
        return ("REFUSED — the deposit item states no native value. A native "
                "bridge that sends an unreadable amount cannot be asserted "
                "against the arrival floor, which was computed from the amount "
                "we declared.")
    if value != int(amount_in_raw):
        return (f"REFUSED — the deposit item sends {value} wei, not the declared "
                f"{amount_in_raw}. That is a different order from the one that "
                f"was priced, and the arrival floor came from the priced one.")

    data = tx_data.get("data")
    if data is not None and not (isinstance(data, str) and data.startswith("0x")):
        return ("REFUSED — the deposit item carries calldata that is not "
                "0x-prefixed hex.")
    return None


class EvmOriginLeg:
    """Build → assert → simulate → size → (send → confirm) for an EVM origin.

    Split in two on purpose: :meth:`prepare` runs everything that can happen
    BEFORE a dry run returns, so a dry run shows the real simulation verdict
    rather than a promise about one. :meth:`send` and :meth:`confirm` are the only
    parts that touch the chain.
    """

    def __init__(self, *, chain: str, signer, rail_factory: Optional[Callable] = None,
                 authorize_fn: Optional[Callable] = None):
        self._chain = chain
        self._signer = signer
        self._rail_factory = rail_factory
        self._authorize_fn = authorize_fn

    def prepare(self, *, tx_data: dict, origin_chain_id: int, amount_in_raw: int,
                amount_usd: Optional[float], gate, execution_context,
                tool, idempotency_key: str) -> PreparedLeg:
        from core.wallet import tx_guard
        from core.wallet.broadcast.evm import EvmRail

        # Every refusal carries its reason IN `header`. The caller prints the
        # header and nothing else, so a verdict is stated exactly once — printing
        # it twice reads like two different problems (seen on the first prod dry
        # run, where the PolicyGate refusal appeared above and below itself).
        problem = assert_order_matches_request(
            tx_data, origin_chain_id=origin_chain_id, amount_in_raw=amount_in_raw)
        if problem:
            return PreparedLeg(False, problem, header=f"  guard: {problem}\n")

        try:
            rail = (self._rail_factory or EvmRail)(chain=self._chain, signer=self._signer)
        except Exception as exc:
            # `EvmRail.__init__` refuses a chain that is unknown or read-only. That
            # is a capability answer, not a transport failure, so it is reported
            # as the reason rather than swallowed.
            return PreparedLeg(False, f"REFUSED — {exc}",
                               header=f"  guard: REFUSED — {exc}\n")

        try:
            tx = rail.build_call(to=str(tx_data["to"]),
                                 data=str(tx_data.get("data") or "0x"),
                                 value=int(str(tx_data["value"])))
        except Exception as exc:
            _why = f"REFUSED — could not build the deposit transaction: {exc}"
            return PreparedLeg(False, _why, header=f"  guard: {_why}\n")

        # `token=None` DECLARES a native send (039 B1). The guard simulates it and
        # asserts the measured native outflow against `amount_raw`; it also prices
        # the outflow itself through the chain's pinned wrapped native, so the USD
        # figure the caps run against is never Relay's claim about our money.
        declared_usd = (float(amount_usd) * (1.0 + VALUATION_TOLERANCE)
                        if amount_usd is not None else 0.0)
        intent = tx_guard.TxIntent(
            chain=self._chain, token=None, to=str(tx_data["to"]),
            amount_raw=int(amount_in_raw),
            max_spend_usd=declared_usd,
            expected_allowance_grants=(), idempotency_key=idempotency_key)

        authorize = self._authorize_fn or tx_guard.authorize
        try:
            from tools.controller.action_registration import (
                _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)
            forged_fn, autonomous_ok_fn = (_is_forged_or_autonomous_turn,
                                           _is_autonomous_goal_turn)
        except Exception:
            forged_fn = autonomous_ok_fn = None

        decision = authorize(
            intent, tx, holder=self._signer.address, gate=gate,
            execution_context=execution_context, tool_self=tool,
            price_fn=getattr(tool, "_price", None),
            fallback_price_fn=getattr(tool, "_fallback_price", None),
            forged_fn=forged_fn, autonomous_ok_fn=autonomous_ok_fn)

        header = (f"  simulated value: "
                  f"{'unknown' if decision.amount_usd is None else f'${decision.amount_usd:.4f}'}\n"
                  f"  guard: {decision.reason}\n  lane:  {decision.lane}\n")

        if not decision.allowed and decision.lane != "owner_queue":
            return PreparedLeg(False, decision.reason, header=header)

        if decision.sim_gas_used:
            try:
                tx = rail.size_gas(tx, decision.sim_gas_used)
            except Exception as exc:
                return PreparedLeg(False, f"REFUSED at gas sizing: {exc}",
                                   header=header + f"  guard: REFUSED at gas "
                                                   f"sizing: {exc}\n")
            header += (f"  gas:   limit {tx.get('gas')} "
                       f"(simulation used {decision.sim_gas_used})\n")

        return PreparedLeg(
            True, decision.reason, rail=rail, tx=tx,
            amount_usd=decision.amount_usd,
            needs_owner_approval=not decision.allowed, header=header)

    @staticmethod
    def send(prepared: PreparedLeg) -> SentLeg:
        try:
            tx_hash = prepared.rail.sign_and_send(prepared.tx)
        except Exception as exc:
            return SentLeg("error", f"broadcast failed: {exc}")
        return SentLeg("sent", "broadcast accepted", tx_hash=tx_hash)

    @staticmethod
    def confirm(prepared: PreparedLeg, tx_hash: str) -> SentLeg:
        """A receipt is the answer; the absence of one is an open question.

        ``status == 0`` is REVERTED and terminal — the fee was paid and the value
        did not move. No receipt inside the budget is ``pending``: the origin may
        still land, and on a bridge a blind re-send pays twice.
        """
        try:
            receipt = prepared.rail.await_receipt(tx_hash)
        except Exception as exc:
            return SentLeg("pending", f"receipt read failed: {exc}", tx_hash=tx_hash)
        if receipt.succeeded:
            return SentLeg("confirmed", f"block {receipt.block_number}", tx_hash=tx_hash)
        if receipt.status == "pending":
            return SentLeg("pending", "no receipt within the timeout", tx_hash=tx_hash)
        return SentLeg("reverted", "receipt status 0", tx_hash=tx_hash)
