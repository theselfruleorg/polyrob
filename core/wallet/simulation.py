"""Delta engine — what a transaction WOULD do, measured rather than assumed.

`tx_guard` bounds a transaction by asserting its observed effects against a
declared intent. For that to mean anything, the effects must be OBSERVED: this
module reads balances and allowances, runs the transaction under `eth_call` with
a state override, reads them again, and reports the differences. It never parses
the caller's calldata to decide what the transaction does — doing so would check
the declaration against itself.

Honest limits, stated rather than papered over:

* **Simulation is not execution.** A contract can behave differently at
  execution time (block-dependent logic, another transaction landing in the same
  block, a contract that detects `eth_call`). Mitigated by simulating at pending
  state and re-simulating immediately pre-broadcast — not eliminated.
* **Simulation cannot see the future.** An allowance granted now can be drained
  in a later transaction this engine will never observe. That is why an
  allowance INCREASE is refused unless explicitly declared, rather than merely
  reported.
* **The RPC is the oracle.** Every number here comes from one endpoint, so a
  lying endpoint yields a lying simulation. `tx_guard` refuses to arm on the
  default public RPC for that reason.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from core.wallet import onchain

logger = logging.getLogger(__name__)

_SEL_BALANCE_OF = "0x70a08231"
_SEL_ALLOWANCE = "0xdd62ed3e"
# keccak("getEthBalance(address)")[:4] — Multicall3's native-balance read, used
# because eth_getBalance cannot appear inside an eth_simulateV1 call list.
_SEL_GET_ETH_BALANCE = "0x4d2301cc"

# Event topics (keccak of the canonical signatures, verified 2026-08-14; the
# non-fungible rows verified 2026-09-15).
#
# ⚠️ _TOPIC_TRANSFER and _TOPIC_APPROVAL are SHARED between ERC-20 and ERC-721 —
# the canonical signature string is literally the same for both, so the keccak is
# the same. The TOPIC COUNT is the only discriminator, and it is exact:
#
#   Transfer  3 topics -> ERC-20  (from, to      | value  in data)
#   Transfer  4 topics -> ERC-721 (from, to, id  | data empty)
#   Approval  3 topics -> ERC-20  (owner, spender| value  in data)
#   Approval  4 topics -> ERC-721 (owner, to, id | data empty)
_TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_TOPIC_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
# ApprovalForAll(address indexed owner, address indexed operator, bool approved)
# — 3 topics, and the payload is a BOOL, never an amount. This is the blanket
# grant: a standing claim on every token of a collection, present and future.
_TOPIC_APPROVAL_FOR_ALL = "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31"
# ⚠️ ERC-1155 indexes the OPERATOR first, so topics[1] is NOT the sender:
#   TransferSingle(operator, from, to | id, value            in data)
#   TransferBatch (operator, from, to | ids[], values[]      in data)
# Reusing the ERC-20 `topics[1] == holder` filter reads these backwards.
_TOPIC_TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
_TOPIC_TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"

#: ERC-1155 `TransferBatch` arrays are bounded so a pathological log cannot turn
#: one simulation into an unbounded allocation. A batch beyond this is malformed
#: for our purposes and skipped like any other unreadable log.
_MAX_BATCH_IDS = 512


@dataclass(frozen=True)
class Deltas:
    """Observed effect of a simulated transaction.

    ``ok=False`` means the simulation could not be trusted (revert, RPC error,
    or an unreadable balance) — the guard must refuse, never proceed on partial
    information.

    ``holder_transfers``/``holder_approvals`` come from the simulated tx's own
    EVENT LOG: ``(token_contract, counterparty, amount)`` for every
    ``Transfer(from=holder, …)`` / ``Approval(owner=holder, …)`` it emitted.
    The balance/allowance READS above only cover the tokens and spenders the
    caller DECLARED — a grant to an undeclared spender or a drain of an
    undeclared token is invisible to them. The log is what the transaction
    actually did to the holder, whoever it touched.
    """
    ok: bool
    native_delta: int = 0
    token_deltas: Dict[str, int] = field(default_factory=dict)
    allowance_deltas: Dict[Tuple[str, str], int] = field(default_factory=dict)
    holder_transfers: Tuple[Tuple[str, str, int], ...] = ()
    holder_approvals: Tuple[Tuple[str, str, int], ...] = ()
    #: NON-FUNGIBLE movement the tx emitted, ``(contract, standard,
    #: counterparty, token_id, amount)``. ``standard`` is ``"erc721"`` |
    #: ``"erc1155"``; ``amount`` is 1 for ERC-721 and the 1155 value. The
    #: token_id is an IDENTIFIER and is deliberately a SEPARATE shape from
    #: ``holder_transfers`` so the existing ``amount > 0`` arithmetic can never
    #: be applied to it. Both directions are matched — until 2026-09-15 only
    #: the holder-as-sender direction was, so an inbound asset was invisible.
    holder_nft_out: Tuple[Tuple[str, str, str, int, int], ...] = ()
    holder_nft_in: Tuple[Tuple[str, str, str, int, int], ...] = ()
    #: ``(contract, operator, approved)`` from ApprovalForAll. ⚠️ The third
    #: element is a BOOL, not an amount — this is why it is NOT folded into
    #: ``holder_approvals``, whose third element is an integer the guard does
    #: arithmetic on.
    holder_operator_grants: Tuple[Tuple[str, str, bool], ...] = ()
    #: ``(contract, approved_to, token_id)`` from a 4-topic Approval — the
    #: ERC-721 single-token approval. Same risk as an allowance, one token wide.
    holder_nft_approvals: Tuple[Tuple[str, str, int], ...] = ()
    #: gasUsed of the simulated tx entry. The rail sizes the broadcast gas
    #: limit from it (a fixed limit out-of-gas-reverts a swap and burns the
    #: fee). None when the node did not report it — never 0.
    gas_used: Optional[int] = None
    #: The simulated tx's own return data, 0x-prefixed (042). For an ordinary
    #: call this is the function's return value and nothing reads it. For a
    #: CREATE it is the DEPLOYED RUNTIME BYTECODE, which is the only way to
    #: prove, before broadcast, that the contract about to exist is the contract
    #: that was declared. None when the node reported none.
    return_data: Optional[str] = None
    error: Optional[str] = None
    event_topics: Tuple[Tuple[str, str], ...] = ()
    logs: Tuple[dict, ...] = ()

    @property
    def grants_allowance(self) -> bool:
        return any(v > 0 for v in self.allowance_deltas.values())


def _default_rpc_for(chain: str):
    """Chain-aware default transport. The old default hardcoded "base", so a
    caller simulating for another chain would silently measure BASE state —
    unreachable today (the trade tool refuses non-base chains) but a landmine
    inside the guard's trust anchor."""
    def _rpc(method: str, params: list, timeout: float = 8.0):
        return onchain._rpc(onchain.rpc_url_for_chain(chain), method, params, timeout)
    return _rpc


def _pad_addr(addr: str) -> str:
    return addr[2:].lower().rjust(64, "0")


def _read_uint(rpc: Callable, chain: str, to: str, data: str) -> Optional[int]:
    raw = rpc("eth_call", [{"to": to, "data": data}, "latest"])
    return onchain._hex_int(raw)


def _balance_of(rpc, chain, token, holder):
    return _read_uint(rpc, chain, token, _SEL_BALANCE_OF + _pad_addr(holder))


def _allowance(rpc, chain, token, owner, spender):
    return _read_uint(rpc, chain, token,
                      _SEL_ALLOWANCE + _pad_addr(owner) + _pad_addr(spender))


def _native_balance(rpc, holder) -> Optional[int]:
    return onchain._hex_int(rpc("eth_getBalance", [holder, "latest"]))


def simulate(tx: dict, *, holder: str, chain: str,
             tokens: List[str], spenders: List[str],
             rpc: Optional[Callable] = None) -> Deltas:
    """Simulate *tx* and return the observed deltas for *holder*.

    ⚠️ Uses ``eth_simulateV1``, NOT a sequence of ``eth_call``s. This is the
    whole correctness of the module: ``eth_call`` does not persist state, so
    reading a balance, running the transaction, and reading it again returns the
    SAME number every time. That produced all-zero deltas, which the guard read
    as "costs nothing" and authorized — a live 0.25 USDC transfer was broadcast
    on 2026-08-09 through a cap that should have refused it. A bundle is the
    only way these reads mean anything.

    ``eth_simulateV1`` executes the calls in order with state carried between
    them, so the layout is: [reads…, THE TX, reads…]. If the node does not
    support it, this refuses — it never falls back to the vacuous form.

    ``tokens`` are the ERC-20 contracts to measure; ``spenders`` are the
    addresses whose allowance over each token should be measured. Anything not
    measured cannot be asserted, so the guard must name every address the
    transaction touches.
    """
    rpc = rpc or _default_rpc_for(chain)

    reads: List[dict] = []
    layout: List[tuple] = []
    # Native balance FIRST, via the pinned Multicall3 aggregator. Before this
    # read existed, `native_delta` was hardcoded 0, so tx_guard's "unexpected
    # native balance change" assertion could never fire — dead code shaped like
    # a defense. An unreadable native balance refuses like any other read.
    from core.wallet.onchain import MULTICALL3
    reads.append({"from": holder, "to": MULTICALL3,
                  "data": _SEL_GET_ETH_BALANCE + _pad_addr(holder)})
    layout.append(("native", None))
    for t in tokens:
        reads.append({"from": holder, "to": t,
                      "data": _SEL_BALANCE_OF + _pad_addr(holder)})
        layout.append(("token", t))
    for t in tokens:
        for s in spenders:
            reads.append({"from": holder, "to": t,
                          "data": _SEL_ALLOWANCE + _pad_addr(holder) + _pad_addr(s)})
            layout.append(("allow", (t, s)))

    the_tx = {"from": holder, "to": tx.get("to"),
              "data": tx.get("data", "0x"),
              "value": hex(int(tx.get("value", 0) or 0))}
    calls = reads + [the_tx] + reads
    tx_index = len(reads)

    try:
        result = rpc("eth_simulateV1", [
            {"blockStateCalls": [{"calls": calls}],
             "validation": False, "traceTransfers": False}, "latest"])
    except Exception as exc:
        return Deltas(ok=False, error=(
            f"simulation unavailable ({exc}) — refusing rather than proceeding "
            f"on an unsimulated transaction"))

    try:
        entries = (result or [{}])[0].get("calls") or []
    except Exception:
        entries = []
    if len(entries) != len(calls):
        return Deltas(ok=False, error=(
            "simulation returned an unexpected shape — refusing "
            f"(expected {len(calls)} results, got {len(entries)})"))

    tx_entry = entries[tx_index]
    if str(tx_entry.get("status")) not in ("0x1", "1"):
        return Deltas(ok=False, error=(
            f"the transaction REVERTS in simulation "
            f"({tx_entry.get('error') or 'status 0'})"))

    # The tx's own event log — the only view that covers tokens and spenders
    # the caller did NOT declare. A missing log SET (key absent, not an empty
    # list) means the node did not report events; refusing is the only honest
    # reading, since "no key" and "no hidden approve" are not the same thing.
    raw_logs = tx_entry.get("logs")
    if raw_logs is None:
        return Deltas(ok=False, error=(
            "simulation returned no event-log set for the transaction — "
            "hidden transfers/approvals cannot be ruled out, refusing"))
    events = _holder_events(raw_logs, holder)
    holder_transfers, holder_approvals = events.transfers, events.approvals

    def _value(entry) -> Optional[int]:
        if str(entry.get("status")) not in ("0x1", "1"):
            return None
        return onchain._hex_int(entry.get("returnData"))

    before = [_value(e) for e in entries[:tx_index]]
    after = [_value(e) for e in entries[tx_index + 1:]]
    if any(v is None for v in before + after):
        return Deltas(ok=False, error=(
            "simulation produced an unknown balance/allowance — refusing rather "
            "than treating unknown as zero"))

    native_delta = 0
    token_deltas: Dict[str, int] = {}
    allowance_deltas: Dict[Tuple[str, str], int] = {}
    for i, (kind, key) in enumerate(layout):
        delta = after[i] - before[i]
        if kind == "native":
            native_delta = delta
        elif kind == "token":
            token_deltas[key] = delta
        else:
            allowance_deltas[key] = delta

    return Deltas(ok=True, native_delta=native_delta,
                  token_deltas=token_deltas, allowance_deltas=allowance_deltas,
                  holder_transfers=holder_transfers,
                  holder_approvals=holder_approvals,
                  holder_nft_out=events.nft_out,
                  holder_nft_in=events.nft_in,
                  holder_operator_grants=events.operator_grants,
                  holder_nft_approvals=events.nft_approvals,
                  gas_used=onchain._hex_int(tx_entry.get("gasUsed")),
                  return_data=tx_entry.get("returnData"),
                  logs=tuple(raw_logs),
                  event_topics=tuple((str(log.get("address", "")).lower(),
                                      str(log["topics"][0]).lower())
                                     for log in raw_logs if log.get("topics")))


@dataclass(frozen=True)
class _HolderEvents:
    """What the simulated tx's log says it did to *holder*, by asset shape."""
    transfers: Tuple[Tuple[str, str, int], ...] = ()
    approvals: Tuple[Tuple[str, str, int], ...] = ()
    nft_out: Tuple[Tuple[str, str, str, int, int], ...] = ()
    nft_in: Tuple[Tuple[str, str, str, int, int], ...] = ()
    operator_grants: Tuple[Tuple[str, str, bool], ...] = ()
    nft_approvals: Tuple[Tuple[str, str, int], ...] = ()


def _addr_of(topic: str) -> str:
    """The 20-byte address packed in a 32-byte indexed topic."""
    return "0x" + topic[-40:]


def _batch_pairs(data: str):
    """``(id, value)`` pairs out of an ERC-1155 TransferBatch data blob.

    Raises on ANY malformation — a mismatched ids/values length, an absurd
    count, a truncated blob — so the caller skips the log rather than reporting
    a movement it could not actually read.
    """
    from core.wallet.abi import decode

    ids, values = decode(
        [{"type": "uint256[]"}, {"type": "uint256[]"}], data)
    if len(ids) != len(values):
        raise ValueError("TransferBatch ids/values length mismatch")
    if len(ids) > _MAX_BATCH_IDS:
        raise ValueError("TransferBatch too large to read")
    return list(zip(ids, values))


def _holder_events(raw_logs, holder: str) -> _HolderEvents:
    """Everything the simulated tx's log says it did to *holder*'s assets.

    Dispatch is on ``(len(topics), topics[0])`` — see the topic table above.
    ⚠️ The count is load-bearing: ERC-20 and ERC-721 share a ``topics[0]``.

    Only well-formed logs are read; a malformed log is skipped (a contract that
    emits garbage cannot be asserted either way — the primary balance/allowance
    reads still stand). A NON-standard token that emits no event at all is
    likewise invisible here; the log layer is defence in depth over the reads,
    not a replacement.
    """
    holder_word = holder[2:].lower().rjust(64, "0")
    transfers = []
    approvals = []
    nft_out = []
    nft_in = []
    operator_grants = []
    nft_approvals = []

    def _is_holder(topic: str) -> bool:
        return topic[2:] == holder_word

    for log in raw_logs or ():
        try:
            topics = [str(t).lower() for t in (log.get("topics") or ())]
            contract = str(log.get("address") or "").lower()
            if not contract or not topics:
                continue
            topic0 = topics[0]
            data = str(log.get("data") or "0x")

            # -- 3 topics: the fungible shapes + the blanket grant ------------
            if len(topics) == 3:
                if topic0 == _TOPIC_APPROVAL_FOR_ALL:
                    # ⚠️ owner is topics[1]; the payload is a BOOL. Never folded
                    # into `approvals`, whose third element is an amount.
                    if not _is_holder(topics[1]):
                        continue
                    operator_grants.append(
                        (contract, _addr_of(topics[2]),
                         bool(int(data, 16) if data not in ("", "0x") else 0)))
                    continue
                if topic0 not in (_TOPIC_TRANSFER, _TOPIC_APPROVAL):
                    continue
                if not _is_holder(topics[1]):
                    continue
                amount = int(data, 16) if data not in ("", "0x") else 0
                counterparty = _addr_of(topics[2])
                if topic0 == _TOPIC_TRANSFER:
                    transfers.append((contract, counterparty, amount))
                else:
                    approvals.append((contract, counterparty, amount))
                continue

            if len(topics) != 4:
                continue

            # -- 4 topics: the non-fungible shapes ---------------------------
            if topic0 == _TOPIC_TRANSFER:
                token_id = int(topics[3], 16)
                if _is_holder(topics[1]):
                    nft_out.append((contract, "erc721", _addr_of(topics[2]),
                                    token_id, 1))
                elif _is_holder(topics[2]):
                    nft_in.append((contract, "erc721", _addr_of(topics[1]),
                                   token_id, 1))
                continue

            if topic0 == _TOPIC_APPROVAL:
                if _is_holder(topics[1]):
                    nft_approvals.append(
                        (contract, _addr_of(topics[2]), int(topics[3], 16)))
                continue

            if topic0 in (_TOPIC_TRANSFER_SINGLE, _TOPIC_TRANSFER_BATCH):
                # ⚠️ topics[1] is the OPERATOR. from/to are topics[2]/topics[3].
                # Being the operator alone moved nothing of OURS.
                if topic0 == _TOPIC_TRANSFER_SINGLE:
                    raw = data[2:] if data.startswith("0x") else data
                    if len(raw) < 128:
                        continue
                    pairs = [(int(raw[:64], 16), int(raw[64:128], 16))]
                else:
                    pairs = _batch_pairs(data)
                if _is_holder(topics[2]):
                    sink, other = nft_out, _addr_of(topics[3])
                elif _is_holder(topics[3]):
                    sink, other = nft_in, _addr_of(topics[2])
                else:
                    continue
                for token_id, value in pairs:
                    sink.append((contract, "erc1155", other, token_id, value))
                continue
        except Exception:
            continue

    return _HolderEvents(
        transfers=tuple(transfers), approvals=tuple(approvals),
        nft_out=tuple(nft_out), nft_in=tuple(nft_in),
        operator_grants=tuple(operator_grants),
        nft_approvals=tuple(nft_approvals))
