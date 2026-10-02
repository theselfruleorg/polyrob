"""``agent_nft_collection_reveal`` — the collection revealer (069 v4): ``reveal(ids)`` on a
pinned collection profile with the ``reveal`` capability (``core/wallet/collection_registry.py``).

A minted token reveals from ``blockhash(revealBlock[id])`` once ``block.number >
revealBlock[id]``; if nobody reveals within 256 L1 blocks (≈51 min) the id re-commits. Every
``mint`` auto-reveals 4 due ids from the queue; this verb reveals the rest. ``reveal`` is
callable by anyone, takes no value and moves no asset: it costs gas only (``reveal(10)``
369,566 gas, ``reveal(1)`` 58,823).

Unlike the other agent_nft verbs its body lives HERE, in core, not in the agent-NFT
package: it reads only the pinned collection (the owner-written collection registry) and a guard shape
core owns (``TxIntent.is_collection_reveal``, ``core/wallet/collection_reveal.py``), and the scheduled
write job (``cron/write_job.py``) runs it with no model turn — it must not depend on an
optional package.

Each run:
1. ``head`` = ``block.number`` as the EVM sees it (Multicall3 ``getBlockNumber`` through
   ``eth_call``). ⚠️ On 4663 that is the L1 block number, NOT ``eth_blockNumber`` (the collection spec).
2. due ids = ids in ``[nextToReveal, nextId − 1]`` with ``revealBlock(id) != 0`` and
   ``revealBlock(id) < head``, in id order, up to ``max_ids``. An unreadable
   ``revealBlock`` stops the run: a due id is never skipped.
3. ``reveal(ids)`` from the treasury key through ``tx_guard.authorize`` — the one authorizer.
4. After the receipt, count ``Revealed`` / ``Recommitted``; any ``Recommitted`` is an
   ``ALERT`` line (the revealer missed ≈51 min).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_GAS_FLAG = "AGENT_NFT_REVEAL_MAX_GAS_USD"
#: The guard prices the WORST case: simulated gas × 1.5 × maxFeePerGas, and the rail's
#: maxFeePerGas is 2 × base fee + tip (``EvmRail._finish_tx``; a zero tip reads as 0.1 gwei).
#: On 4663 (base ≈ 0.024 gwei) ``reveal(10)`` is then ≈ 555k × 0.148 gwei ≈ 8.2e-5 ETH
#: ≈ $0.33 at $4,000/ETH, while the fee actually paid is ≈ 370k × 0.024 gwei ≈ $0.04.
DEFAULT_MAX_GAS_USD = 1.0
DEFAULT_MAX_IDS = 10
#: ``revealBlock`` reads per Multicall3 ``aggregate3`` call.
_SCAN_CHUNK = 200

_SEL_GET_BLOCK_NUMBER = "0x42cbb15c"   # Multicall3.getBlockNumber()


class RevealError(RuntimeError):
    """A reveal run that cannot proceed; the text is the reason shown."""


def max_gas_usd() -> float:
    """``AGENT_NFT_REVEAL_MAX_GAS_USD`` — the most USD of FEE one reveal transaction may cost
    (default 1.00). The guard prices the worst-case fee (simulated gas × 1.5 × maxFeePerGas)
    and refuses above it. A non-finite or non-positive value disables the verb (0)."""
    import math
    from core.env import float_env
    v = float_env(MAX_GAS_FLAG, DEFAULT_MAX_GAS_USD)
    return v if (math.isfinite(v) and v > 0) else 0.0


def _selectors():
    from core.wallet import abi
    return {"nextId": abi.selector("nextId()"),
            "nextToReveal": abi.selector("nextToReveal()"),
            "revealBlock": abi.selector("revealBlock(uint256)")}


def _uint(raw) -> int:
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 3:
        raise RevealError(f"unreadable uint256 {raw!r}")
    return int(raw, 16)


def _call(rpc, to: str, data: str) -> int:
    return _uint(rpc("eth_call", [{"to": to, "data": data}, "latest"]))


def read_head(rpc) -> int:
    """``block.number`` inside the EVM (Multicall3 ``getBlockNumber``) — the number the
    contract compares ``revealBlock`` with. On Arbitrum-stack chains this is the L1 block
    number, which ``eth_blockNumber`` is NOT."""
    from core.wallet.onchain import MULTICALL3
    return _call(rpc, MULTICALL3, _SEL_GET_BLOCK_NUMBER)


def read_reveal_blocks(rpc, collection: str, ids: List[int]) -> List[Optional[int]]:
    """``revealBlock(id)`` for each id through Multicall3 ``aggregate3``; ``None`` = unread."""
    from core.wallet import onchain
    sel = _selectors()["revealBlock"]
    out: List[Optional[int]] = []
    for at in range(0, len(ids), _SCAN_CHUNK):
        chunk = ids[at:at + _SCAN_CHUNK]
        data = onchain._encode_aggregate3(
            [(collection, sel + format(int(i), "064x")) for i in chunk])
        raw = rpc("eth_call", [{"to": onchain.MULTICALL3, "data": data}, "latest"])
        out.extend(onchain._decode_aggregate3_result(raw, len(chunk)))
    return out


def due_ids(rpc, collection: str, *, head: int, max_ids: int) -> Tuple[List[int], int, int]:
    """``(due, next_to_reveal, next_id)``. Raises RevealError on any unreadable value."""
    sels = _selectors()
    next_to_reveal = _call(rpc, collection, sels["nextToReveal"])
    next_id = _call(rpc, collection, sels["nextId"])
    if next_to_reveal < 1 or next_id < 1:
        raise RevealError(f"the collection reads nextToReveal={next_to_reveal}, nextId={next_id}; "
                          f"both start at 1 (the collection spec) — this is not a collection with the reveal shape")
    candidates = list(range(next_to_reveal, next_id))
    due: List[int] = []
    for at in range(0, len(candidates), _SCAN_CHUNK):
        chunk = candidates[at:at + _SCAN_CHUNK]
        for token_id, rb in zip(chunk, read_reveal_blocks(rpc, collection, chunk)):
            if rb is None:
                raise RevealError(f"revealBlock({token_id}) could not be read — a due id is never "
                                  f"skipped, so this run stops")
            if rb != 0 and rb < head:
                due.append(token_id)
                if len(due) >= max_ids:
                    return due, next_to_reveal, next_id
    return due, next_to_reveal, next_id


def pinned_collection(chain: str, requested: Optional[str] = None) -> str:
    from core.wallet import collection_reveal as R
    try:
        pinned = R.pinned_collections(chain)
    except Exception as exc:  # noqa: BLE001 — an untrusted registry pins nothing
        raise RevealError(f"the owner's collection registry cannot be trusted ({exc})") from exc
    if not pinned:
        raise RevealError(f"no collection with the reveal capability is pinned on {chain} (the "
                          f"owner's collection registry is empty by default)")
    if requested:
        if requested.lower() not in pinned:
            raise RevealError(f"{requested} is not a pinned collection on {chain}")
        return requested.lower()
    if len(pinned) > 1:
        raise RevealError(f"{len(pinned)} collections are pinned on {chain}; name one "
                          f"(`collection`)")
    return pinned[0]


def _receipt_events(rpc, collection: str, tx_hash: str) -> Tuple[List[int], List[int]]:
    from core.wallet import collection_reveal as R
    rec = rpc("eth_getTransactionReceipt", [tx_hash]) or {}
    revealed, recommitted = [], []
    for log in rec.get("logs") or ():
        if str(log.get("address") or "").lower() != collection:
            continue
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if len(topics) >= 2 and topics[0] == R.TOPIC_REVEALED:
            revealed.append(int(topics[1], 16))
        elif len(topics) >= 2 and topics[0] == R.TOPIC_RECOMMITTED:
            recommitted.append(int(topics[1], 16))
    return revealed, recommitted


def _paid_usd(rpc, tx_hash: str, tx: dict, worst_usd) -> float:
    """The fee actually PAID, in USD, for the spend record: the guard's worst-case figure
    scaled by ``gasUsed × effectiveGasPrice / (gas limit × maxFeePerGas)`` from the receipt.
    The revealer runs every minute, and booking the worst case (≈8× the real fee on 4663)
    against the rolling daily cap would starve every other money verb. Any unreadable
    field books the worst case — never less, never $0.00."""
    worst = float(worst_usd or 0.0)
    try:
        rec = rpc("eth_getTransactionReceipt", [tx_hash]) or {}
        paid = int(rec["gasUsed"], 16) * int(rec["effectiveGasPrice"], 16)
        budget = int(tx.get("gas") or 0) * int(tx.get("maxFeePerGas") or 0)
        if paid <= 0 or budget <= 0 or paid > budget:
            return worst
        return max(0.01, round(worst * paid / budget, 4)) if worst > 0 else worst
    except Exception:  # noqa: BLE001
        return worst


def _price(chain, addr):
    # 071: the one read layer; a DISPUTED quote yields None.
    from tools.defi.price_sources import indexer_price
    return indexer_price(chain, addr)


async def run(tool, params, execution_context):
    """The verb body. Returns the tool's ActionResult; never raises."""
    from core.wallet import collection_reveal as R
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet.tx_guard import TxIntent

    chain = params.chain
    cap = max_gas_usd()
    spend_line = (f"  spends:   gas only, from the treasury key; at most ${cap:.2f} of fee "
                  f"({MAX_GAS_FLAG}); no value, no asset\n")
    if cap <= 0:
        return tool._ar(error=f"{MAX_GAS_FLAG} is 0 or invalid — the revealer is disabled. "
                              f"Nothing was broadcast.")
    max_spend = min(float(params.max_spend_usd or cap), cap)
    rpc = tool.rpc_for(chain)
    try:
        collection = pinned_collection(chain, params.collection)
        head = await asyncio.to_thread(read_head, rpc)
        ids, ntr, nid = await asyncio.to_thread(
            due_ids, rpc, collection, head=head, max_ids=int(params.max_ids))
    except Exception as exc:  # noqa: BLE001 — every read failure is a reported refusal
        return tool._ar(error=f"agent_nft_collection_reveal: {exc}. Nothing was broadcast.")
    header = (f"COLLECTION REVEAL on {chain} {collection}: head (EVM block.number) {head}, "
              f"nextToReveal {ntr}, minted {nid - 1}\n" + spend_line)
    if not ids:
        return tool._ar(content=header + "  due:      none — nothing to reveal, no transaction.")
    header += f"  due:      {len(ids)} id(s) {ids[0]}..{ids[-1]}: {ids}\n"

    wallet = tool._get_wallet()
    if wallet is None:
        return tool._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED). Nothing was broadcast.")
    signer = wallet.operational_signer()
    gate = wallet.policy
    data = R.encode_reveal(ids)
    idem = "agent_nft_collection_reveal:{}:{}".format(chain, hashlib.sha256(
        f"{collection}|{head}|{','.join(map(str, ids))}".encode()).hexdigest()[:24])
    try:
        rail = (getattr(tool, "_rail_factory", None) or EvmRail)(chain=chain, signer=signer)
        tx = rail.build_call(to=collection, data=data, value=0)
    except Exception as exc:  # noqa: BLE001
        return tool._ar(error=f"could not build the transaction: {exc}. Nothing was broadcast.")
    intent = TxIntent(chain=chain, token=None, to=collection, amount_raw=0,
                      max_spend_usd=max_spend, idempotency_key=idem,
                      is_collection_reveal=True, reveal_ids=tuple(ids))

    async with gate.reserve():
        from tools.controller.action_registration import (_is_autonomous_goal_turn,
                                                          _is_forged_or_autonomous_turn)
        decision = await asyncio.to_thread(
            tool.guard, intent, tx, holder=signer.address, gate=gate,
            execution_context=execution_context, tool_self=tool,
            price_fn=getattr(tool, "_price_fn", None) or _price,
            forged_fn=_is_forged_or_autonomous_turn, autonomous_ok_fn=_is_autonomous_goal_turn,
            account_rpc=rpc)
        usd = "unknown" if decision.amount_usd is None else f"${decision.amount_usd:.4f}"
        header += (f"  cost:     {usd} — the worst-case gas fee\n"
                   f"  guard:    {decision.reason}\n  lane:     {decision.lane}\n")
        if not decision.allowed:
            return tool._ar(content=header + "  RESULT: NOT SENT — nothing was broadcast.")
        if decision.sim_gas_used:
            try:
                tx = rail.size_gas(tx, decision.sim_gas_used)
            except Exception as exc:  # noqa: BLE001
                return tool._ar(error=f"refused at gas sizing: {exc} — nothing was broadcast")
        if params.dry_run:
            return tool._ar(content=header + ("  RESULT: DRY RUN — the guard would allow this, "
                                              "but nothing was broadcast."))
        try:
            tx_hash = await asyncio.to_thread(rail.sign_and_send, tx)
        except Exception as exc:  # noqa: BLE001
            from core.wallet.broadcast.evm import broadcast_failure_text
            return tool._ar(error="ALERT agent_nft_collection_reveal: " + broadcast_failure_text(exc))
        receipt = await asyncio.to_thread(rail.await_receipt, tx_hash)
        paid_usd = await asyncio.to_thread(_paid_usd, rpc, tx_hash, tx, decision.amount_usd)
        gate.record(venue="defi", action="agent_nft_collection_reveal", amount_usd=paid_usd,
                    counterparty=collection, idempotency_key=idem, result_ref=tx_hash, chain=chain)
    if not receipt.succeeded:
        state = "in flight" if receipt.status == "pending" else "REVERTED"
        return tool._ar(content=header + f"ALERT agent_nft_collection_reveal: tx {tx_hash} {state} — reported once; "
                                         f"the next run re-reads the due ids (never a changed list "
                                         f"chosen to favour an outcome)")
    try:
        revealed, recommitted = await asyncio.to_thread(_receipt_events, rpc, collection, tx_hash)
    except Exception as exc:  # noqa: BLE001
        return tool._ar(content=header + f"  tx:       {tx_hash} (confirmed); the receipt's events "
                                         f"could not be read ({exc})")
    out = header + (f"  tx:       {tx_hash} (confirmed), gas used {receipt.gas_used}\n"
                    f"  result:   revealed {len(revealed)} {revealed}; recommitted "
                    f"{len(recommitted)} {recommitted}")
    if recommitted:
        out += (f"\nALERT agent_nft_collection_reveal: {len(recommitted)} id(s) RE-COMMITTED {recommitted} — they "
                f"waited more than 256 L1 blocks (≈51 min) unrevealed")
    return tool._ar(content=out)
