"""``agent_nft_collection_reveal`` — the collection revealer (069 v4): ``reveal(ids)`` on a
pinned collection profile with the ``reveal`` capability (``core/wallet/collection_registry.py``).

A minted token reveals from ``blockhash(revealBlock[id])`` once ``block.number >
revealBlock[id]``; if nobody reveals within 256 L1 blocks (≈51 min) the id re-commits. Every
``mint`` auto-reveals 4 due ids from the queue; this verb reveals the rest. ``reveal`` is
callable by anyone, takes no value and moves no asset: it costs gas only (rig 2026-11-12:
``reveal(10)`` 370,242 gas, ``reveal(64)`` 2,238,231 — ≈34,600 per id).

Unlike the other agent_nft verbs its body lives HERE, in core, not in the agent-NFT
package: it reads only the pinned collection (the owner-written collection registry) and a guard shape
core owns (``TxIntent.is_collection_reveal``, ``core/wallet/collection_reveal.py``), and the scheduled
write job (``cron/write_job.py``) runs it with no model turn — it must not depend on an
optional package.

Each run:
1. ``head`` = ``block.number`` as the EVM sees it (Multicall3 ``getBlockNumber`` through
   ``eth_call``). ⚠️ On 4663 that is the L1 block number, NOT ``eth_blockNumber`` (the collection spec).
2. due ids = ids in ``[nextToReveal, nextId − 1]`` with ``revealBlock(id) != 0`` and
   ``revealBlock(id) < head``. An unreadable ``revealBlock`` stops the run: a due id is never
   skipped. They are taken OLDEST-DUE FIRST (115 B8): the ids whose 256-block window ends
   soonest, then the ids already past it (those only re-commit, so they lose nothing by
   waiting behind an id that can still reveal).
3. The batch is SIZED to the fee cap (115 B8): the guard's own simulation (else the node's
   ``eth_estimateGas``) measures ``reveal`` of the first ``MAX_REVEAL_IDS`` (64, the guard's
   per-transaction maximum) candidates, and the run takes
   as many as the guard's worst-case fee (gas × 1.5 × maxFeePerGas) lets fit under
   ``AGENT_NFT_REVEAL_MAX_GAS_USD`` — ≈30 at 4663 prices and the $1.00 default, where a fixed
   10 left a sell-out's ~3,850-id backlog to re-commit. An explicit ``max_ids`` is an upper
   bound on top. The calldata lists the batch in id order (the guard's shape).
4. ``reveal(ids)`` from the treasury key through ``tx_guard.authorize`` — the one authorizer.
5. After the receipt, count ``Revealed`` / ``Recommitted``; any ``Recommitted`` is an
   ``ALERT`` line (the revealer missed ≈51 min). A run that leaves due ids says how many, and
   how many of them pass their window before the next run (``NEXT_RUN_BLOCKS``).
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
#: The ids per run when the node cannot estimate the batch AND no price sizes it — the old fixed
#: default. The verb normally sizes the batch to the fee cap (:func:`batch_size`).
DEFAULT_MAX_IDS = 10
#: The gas model when the node cannot estimate the batch, rounded UP from the rig (2026-11-12,
#: the current contracts): reveal(10) 370,242 and reveal(64) 2,238,231 gas → 34,592 per id and
#: ≈24,300 fixed. With an estimate, the fixed part stays this and the per-id part is measured.
REVEAL_GAS_FIXED = 25_000
REVEAL_GAS_PER_ID = 35_000
#: ``blockhash`` reads only the last 256 blocks: an id unrevealed by ``revealBlock + 256``
#: re-commits (CommitRevealSeed._revealOne).
REVEAL_WINDOW_BLOCKS = 256
#: Blocks until the next run, for the backlog line: the write job runs every minute and on 4663
#: ``block.number`` is the L1 number (12 s blocks) — 5 blocks.
NEXT_RUN_BLOCKS = 5
#: The share of the fee cap a batch is sized to: the guard rounds its figure UP to the cent,
#: and the simulation's gas differs slightly from the node's estimate.
_CAP_SHARE = 0.9
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


def _scan(rpc, collection: str, *, head: int, max_ids: Optional[int]
          ) -> Tuple[List[Tuple[int, int]], int, int]:
    """``([(id, revealBlock)] due in id order, next_to_reveal, next_id)``; stops at *max_ids*
    (None = the whole queue). Raises RevealError on any unreadable value."""
    sels = _selectors()
    next_to_reveal = _call(rpc, collection, sels["nextToReveal"])
    next_id = _call(rpc, collection, sels["nextId"])
    if next_to_reveal < 1 or next_id < 1:
        raise RevealError(f"the collection reads nextToReveal={next_to_reveal}, nextId={next_id}; "
                          f"both start at 1 (the collection spec) — this is not a collection with the reveal shape")
    candidates = list(range(next_to_reveal, next_id))
    due: List[Tuple[int, int]] = []
    for at in range(0, len(candidates), _SCAN_CHUNK):
        chunk = candidates[at:at + _SCAN_CHUNK]
        for token_id, rb in zip(chunk, read_reveal_blocks(rpc, collection, chunk)):
            if rb is None:
                raise RevealError(f"revealBlock({token_id}) could not be read — a due id is never "
                                  f"skipped, so this run stops")
            if rb != 0 and rb < head:
                due.append((token_id, rb))
                if max_ids is not None and len(due) >= max_ids:
                    return due, next_to_reveal, next_id
    return due, next_to_reveal, next_id


def due_ids(rpc, collection: str, *, head: int, max_ids: int) -> Tuple[List[int], int, int]:
    """``(due, next_to_reveal, next_id)``: due ids in id order, up to *max_ids*. Raises
    RevealError on any unreadable value."""
    due, ntr, nid = _scan(rpc, collection, head=head, max_ids=max_ids)
    return [i for i, _rb in due], ntr, nid


def due_entries(rpc, collection: str, *, head: int) -> Tuple[List[Tuple[int, int]], int, int]:
    """``([(id, revealBlock)] for EVERY due id, next_to_reveal, next_id)``."""
    return _scan(rpc, collection, head=head, max_ids=None)


def expired(reveal_block: int, head: int) -> bool:
    """True when a reveal sent now can no longer read ``blockhash(reveal_block)``: the
    transaction lands at ``head + 1`` or later, and the hash lives for 256 blocks."""
    return int(reveal_block) + REVEAL_WINDOW_BLOCKS <= int(head)


def oldest_due_first(entries: List[Tuple[int, int]], head: int) -> List[Tuple[int, int]]:
    """The order a batch is taken in: the ids still inside their window, the soonest-ending
    first (lowest ``revealBlock``, then id); then the ids already past it (they only
    re-commit, so they wait behind every id that can still reveal)."""
    live = sorted((e for e in entries if not expired(e[1], head)), key=lambda e: (e[1], e[0]))
    dead = sorted((e for e in entries if expired(e[1], head)), key=lambda e: (e[1], e[0]))
    return live + dead


def batch_size(*, limit: int, probe_ids: int = 0, probe_gas: Optional[int] = None,
               max_fee_wei: int, unit_price: Optional[float], max_spend_usd: float,
               decimals: int = 18) -> Tuple[int, int, bool]:
    """``(ids, gas_per_id, priced)``: how many due ids one reveal may take.

    The guard prices a reveal at its WORST-CASE fee, gas × 1.5 × maxFeePerGas, and refuses it
    above the cap; so the batch is the most ids whose modelled gas (``REVEAL_GAS_FIXED`` + the
    per-id gas) keeps that fee under ``_CAP_SHARE`` of *max_spend_usd*, at most *limit* (the
    guard's 64, or a smaller explicit ``max_ids``) and at least 1 (an id the cap cannot pay
    for is the guard's refusal to name, not a silent zero). The per-id gas comes from the
    measured gas of *probe_ids* ids (*probe_gas*), else ``REVEAL_GAS_PER_ID``. No price or
    a $0 fee (a valueless testnet) leaves the cap nothing to bound: *limit* ids, priced=False.
    """
    import math
    limit = max(0, int(limit))
    per = REVEAL_GAS_PER_ID
    if probe_gas and probe_ids > 0 and probe_gas > REVEAL_GAS_FIXED:
        per = max(1, math.ceil((int(probe_gas) - REVEAL_GAS_FIXED) / int(probe_ids)))
    if limit == 0:
        return 0, per, False
    if (unit_price is None or not math.isfinite(unit_price) or unit_price <= 0
            or int(max_fee_wei or 0) <= 0):
        return limit, per, False
    usd_per_gas = 1.5 * int(max_fee_wei) * float(unit_price) / 10 ** int(decimals)
    budget_gas = float(max_spend_usd) * _CAP_SHARE / usd_per_gas
    fit = int((budget_gas - REVEAL_GAS_FIXED) // per)
    return max(1, min(limit, fit)), per, True


def native_usd(chain: str, price_fn) -> Optional[float]:
    """The native coin's spend price as the GUARD reads it (the chain's wrapped native through
    the same valueless-testnet rule and the same no-zero rule), so the batch is sized against
    the figure that will be checked. None = unpriced."""
    try:
        from core.wallet import chains
        from core.wallet.tx_guard import _valueless_price_fn
        from core.wallet.tx_guard_steps.value import _spend_price
        row = chains.get(chain)
        proxy = getattr(row, "wrapped_native", None) if row else None
        if not proxy:
            return None
        priced = _valueless_price_fn(chain, price_fn)
        return _spend_price(priced(chain, proxy) if priced else None, chain)
    except Exception:  # noqa: BLE001 — an unread price sizes nothing; the guard still decides
        return None


def _explicit_max_ids(params) -> Optional[int]:
    """The caller's ``max_ids`` when it SET one (a pydantic model's ``model_fields_set``), else
    None: the field's own default does not bound a sized batch."""
    value = getattr(params, "max_ids", None)
    if value is None:
        return None
    fields = getattr(params, "model_fields_set", None)
    if fields is not None and "max_ids" not in fields:
        return None
    return int(value)


def _max_per_tx() -> int:
    from core.wallet.collection_reveal import MAX_REVEAL_IDS
    return MAX_REVEAL_IDS


def _probe_gas(rpc, rail, tx: dict, *, holder: str, chain: str) -> Tuple[Optional[int], str]:
    """``(gas, source)`` of the candidate batch *tx*: the gasUsed of the SAME simulation the
    guard runs and prices (``eth_simulateV1``), else the node's ``eth_estimateGas`` (a little
    higher: the least limit that succeeds), else ``(None, "gas model")``."""
    try:
        from core.wallet import simulation
        used = simulation.simulate(tx, holder=holder, chain=chain, tokens=[], spenders=[],
                                   rpc=rpc).gas_used
        if used and int(used) > 0:
            return int(used), "simulation"
    except Exception as exc:  # noqa: BLE001 — the probe only sizes; the guard still simulates
        logger.info("reveal batch simulation failed, trying eth_estimateGas: %s", exc)
    est = getattr(rail, "estimate_gas", None)
    if est is not None:
        try:
            got = est({"to": tx.get("to"), "data": tx.get("data"), "value": 0})
            if got and int(got) > 0:
                return int(got), "node estimate"
        except Exception as exc:  # noqa: BLE001
            logger.info("reveal batch estimate failed, the gas model stands in: %s", exc)
    return None, "gas model"


def backlog_line(rest: List[Tuple[int, int]], head: int) -> str:
    """What a run leaves due: how many, how many pass their window before the next run, and
    how many are already past it."""
    if not rest:
        return "  backlog:  none — every due id is in this run\n"
    past = sum(1 for _i, rb in rest if expired(rb, head))
    lapsing = sum(1 for _i, rb in rest
                  if not expired(rb, head) and expired(rb, head + NEXT_RUN_BLOCKS))
    line = (f"  backlog:  {len(rest)} due id(s) remain after this run; {lapsing} pass their "
            f"{REVEAL_WINDOW_BLOCKS}-block window before the next run (≈{NEXT_RUN_BLOCKS} "
            f"blocks, one minute) and will re-commit")
    if past:
        line += f"; {past} already passed it (they re-commit when revealed)"
    if lapsing:
        line += (f"\n  WARNING:  the revealer is behind — {lapsing} id(s) re-commit before the "
                 f"next run unless it runs more often or takes more ids per run ({MAX_GAS_FLAG}; "
                 f"at most {_max_per_tx()} per transaction)")
    return line + "\n"


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
    from tools.defi.price_sources import spend_price
    return spend_price(chain, addr)


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
    explicit = _explicit_max_ids(params)
    limit = min(R.MAX_REVEAL_IDS, explicit) if explicit else R.MAX_REVEAL_IDS
    rpc = tool.rpc_for(chain)
    try:
        collection = pinned_collection(chain, params.collection)
        head = await asyncio.to_thread(read_head, rpc)
        entries, ntr, nid = await asyncio.to_thread(due_entries, rpc, collection, head=head)
    except Exception as exc:  # noqa: BLE001 — every read failure is a reported refusal
        return tool._ar(error=f"agent_nft_collection_reveal: {exc}. Nothing was broadcast.")
    header = (f"COLLECTION REVEAL on {chain} {collection}: head (EVM block.number) {head}, "
              f"nextToReveal {ntr}, minted {nid - 1}\n" + spend_line)
    if not entries:
        return tool._ar(content=header + "  due:      none — nothing to reveal, no transaction.")
    ordered = oldest_due_first(entries, head)
    candidates = ordered[:limit]

    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        return tool._ar(error=WALLET_OFF_TEXT + ". Nothing was broadcast.")
    signer = wallet.operational_signer()
    gate = wallet.policy
    price_fn = getattr(tool, "_price_fn", None) or _price
    try:
        rail = (getattr(tool, "_rail_factory", None) or EvmRail)(chain=chain, signer=signer)
        tx = rail.build_call(to=collection, data=R.encode_reveal(sorted(i for i, _ in candidates)),
                             value=0)
    except Exception as exc:  # noqa: BLE001
        return tool._ar(error=f"could not build the transaction: {exc}. Nothing was broadcast.")
    # 115 B8: as many due ids as the fee cap lets fit, oldest-due first — never a fixed 10.
    probe_gas, probe_src = await asyncio.to_thread(_probe_gas, rpc, rail, tx,
                                                   holder=signer.address, chain=chain)
    unit_price = await asyncio.to_thread(native_usd, chain, price_fn)
    n, per_id, priced = batch_size(limit=len(candidates), probe_ids=len(candidates),
                                   probe_gas=probe_gas, max_fee_wei=int(tx.get("maxFeePerGas") or 0),
                                   unit_price=unit_price, max_spend_usd=max_spend)
    if not probe_gas and not priced:
        # Nothing measured and nothing priced: the old fixed batch, never 64 blind.
        n = min(n, explicit or DEFAULT_MAX_IDS)
    ids = sorted(i for i, _ in candidates[:n])
    if n < len(candidates):
        tx["data"] = R.encode_reveal(ids)
    bound = (f"{explicit} (max_ids)" if explicit and explicit < R.MAX_REVEAL_IDS
             else f"{R.MAX_REVEAL_IDS} per transaction")
    header += (f"  due:      {len(ordered)} id(s); this run takes {len(ids)}, oldest-due first: "
               f"{ids}\n"
               f"  sizing:   ≈{per_id} gas per id ({probe_src}); "
               + (f"{n} id(s) fit {_CAP_SHARE:.0%} of the ${max_spend:.2f} fee cap at "
                  f"maxFeePerGas {int(tx.get('maxFeePerGas') or 0)} wei (bound {bound})\n" if priced
                  else f"the fee is unpriced or $0 here, so the batch is bound by {bound}\n")
               + backlog_line(ordered[n:], head))
    idem = "agent_nft_collection_reveal:{}:{}".format(chain, hashlib.sha256(
        f"{collection}|{head}|{','.join(map(str, ids))}".encode()).hexdigest()[:24])
    intent = TxIntent(chain=chain, token=None, to=collection, amount_raw=0,
                      max_spend_usd=max_spend, idempotency_key=idem,
                      is_collection_reveal=True, reveal_ids=tuple(ids))

    async with gate.reserve():
        from tools.controller.action_registration import (_is_autonomous_goal_turn,
                                                          _is_forged_or_autonomous_turn)
        decision = await asyncio.to_thread(
            tool.guard, intent, tx, holder=signer.address, gate=gate,
            execution_context=execution_context, tool_self=tool,
            price_fn=price_fn,
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
        # CLI1: a cancel in the receipt wait or the paid-fee read records the
        # broadcast first, at the worst-case fee (never under) — the ONE seam.
        from tools.defi.receipt_wait import record_on_interrupt
        with record_on_interrupt(
                tx_hash, gate=gate,
                record_kw=dict(venue="defi", action="agent_nft_collection_reveal",
                               amount_usd=decision.amount_usd or 0.0, counterparty=collection,
                               idempotency_key=idem, result_ref=tx_hash, chain=chain)):
            receipt = await asyncio.to_thread(rail.await_receipt, tx_hash)
            paid_usd = await asyncio.to_thread(_paid_usd, rpc, tx_hash, tx, decision.amount_usd)
        from tools.defi.receipt_wait import book_after_receipt
        book_after_receipt(gate, receipt, venue="defi", action="agent_nft_collection_reveal",
                           amount_usd=paid_usd, counterparty=collection, idempotency_key=idem,
                           result_ref=tx_hash, chain=chain)
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
