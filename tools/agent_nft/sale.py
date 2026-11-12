"""``agent_nft_sale_claim`` / ``agent_nft_sale_settle`` and the sale lines of the account view —
the money the pinned SALE of a collection owes this treasury (proposal 115 W8, item 5).

Core-owned (no agent-NFT package): they read only the owner's collection registry and use a
guard shape core owns (the CLAIM shape restricted to a pinned sale, ``core/wallet/collection_sale.py``).

The contract (polyrob-desk ``PolyrobSale``, SPEC §6.7): an auction mint keeps the whole value; at
``settle(ids)`` (anyone, after ``settled()``) each record's excess ``(paidEach − clearing) × qty``
is PUSHED to the record's ``to`` — this treasury, for the agent's own mints — and the odd wei of
a mint (or a push that failed) waits in ``owed[to]`` until ``claim(to)``. So:

* settle — the treasury's own open records, found from the sale's ``Bid`` logs (no field is
  indexed: the logs are read whole and filtered on ``to``) and confirmed by ``record(i)``;
  ``min_native_inflow_wei`` = the sum of their refunds. Records that refund nothing still get
  their accounts' top-ups from ANY settle (the project's settle job); alone they are no claim.
* claim — ``claim(treasury)`` with ``min_native_inflow_wei`` = ``owed(treasury)``.

An unreadable value is named, never read as "nothing owed".
"""
from __future__ import annotations

from typing import List, Optional

from core.wallet import collection_sale as S

UNREADABLE = "unreadable"
PHASES = {0: "not started", 1: "whitelist", 2: "auction", 3: "settled"}


class SaleError(RuntimeError):
    """The verb cannot proceed; the text is the reason shown."""


def pinned_sale(chain: str, requested: Optional[str] = None):
    """The pinned profile with the ``mint`` capability (and so a ``sale``) on *chain*."""
    from core.wallet import collection_registry as registry
    try:
        profiles = registry.profiles_on(registry.chain_id_of(chain), "mint")
    except Exception as exc:  # noqa: BLE001 — an untrusted registry pins nothing
        raise SaleError(f"the owner's collection registry cannot be trusted ({exc})") from exc
    profiles = [p for p in profiles if p.sale is not None]
    if not profiles:
        raise SaleError(f"no collection with a pinned sale is on {chain} (the owner's collection "
                        f"registry is empty by default)")
    if requested:
        hit = next((p for p in profiles if p.address == requested.lower()), None)
        if hit is None:
            raise SaleError(f"{requested} is not a pinned collection with a sale on {chain}")
        return hit
    if len(profiles) > 1:
        raise SaleError(f"{len(profiles)} collections with a sale are pinned on {chain}; name one "
                        f"(`collection`)")
    return profiles[0]


def _try(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a failed read is reported, never zeroed
        return UNREADABLE


def sale_lines(rpc, profile, *, treasury: Optional[str] = None,
               account: Optional[str] = None) -> List[str]:
    """The view's sale lines: ``phase()``, ``settled()`` and ``owed(who)`` for the treasury and
    the account. Empty for a profile with no sale; a code that does not match its pin is named
    and nothing of it is read."""
    from core.wallet import collection_registry as registry
    if profile is None or getattr(profile, "sale", None) is None:
        return []
    sale = profile.sale.address
    why = _try(lambda: registry.sale_runtime_refusal(rpc, profile))
    if why:
        return [f"  sale:      {sale} — NOT READ: "
                + ("its code could not be read" if why == UNREADABLE else why)]
    phase = _try(lambda: S.read_phase(rpc, sale))
    settled = _try(lambda: S.read_settled(rpc, sale))
    out = [f"  sale:      {sale}, phase {phase}"
           + (f" ({PHASES.get(phase, '?')})" if isinstance(phase, int) else "")
           + f", settled {settled}"]
    for who, label in ((treasury, "the treasury"), (account, "this account")):
        if who:
            owed = _try(lambda w=who: S.read_owed(rpc, sale, w))
            out.append(f"  owed:      {owed if owed == UNREADABLE else f'{owed} wei'} to {label} "
                       + ("(UNREADABLE is not 'nothing owed')" if owed == UNREADABLE
                          else "(agent_nft_sale_claim takes it)" if owed else ""))
    return out


def own_open_records(rpc, profile, treasury: str, *, head: int,
                     requested: Optional[List[int]] = None):
    """``[(id, qty, paidEach)]`` of the treasury's records that are not done. With *requested*
    only those ids are read; else every ``Bid`` log of the sale from the profile's deploy block
    names the candidates. Raises on any unreadable value (a record is never skipped unread)."""
    from tools.agent_nft.view import _logs
    sale = profile.sale.address
    me = treasury.lower()
    if requested:
        ids = sorted(set(int(i) for i in requested))
    else:
        ids = []
        for log in _logs(rpc, address=sale, topics=[S.TOPIC_BID],
                         from_block=int(profile.deploy_block), to_block=int(head)):
            words = S._data_words(log)
            if len(words) == 5 and S._word_addr(words[1]) == me:
                ids.append(int(words[0], 16))
        ids = sorted(set(ids))
    out = []
    for i in ids:
        to, _first, qty, paid, done = S.read_record(rpc, sale, i)
        if to == me and not done:
            out.append((i, qty, paid))
        elif requested and to != me:
            raise SaleError(f"record {i} belongs to {to}, not this treasury")
    return out


def _wei(n: int) -> str:
    return f"{n} wei ({n / 1e18:.6f} ETH)"


def _signer_address(tool) -> str:
    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        raise SaleError(WALLET_OFF_TEXT)
    return wallet.operational_signer().address


async def claim(tool, params, execution_context):
    import asyncio
    from core.wallet import collection_mint
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    chain = params.chain
    rpc = tool.rpc_for(chain)
    try:
        treasury = _signer_address(tool)
        profile = pinned_sale(chain, params.collection)
        sale = profile.sale.address
        why = await asyncio.to_thread(collection_mint.runtime_refusal, chain, sale, rpc)
        if why:
            raise SaleError(why)
    except SaleError as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    try:
        owed = await asyncio.to_thread(S.read_owed, rpc, sale, treasury)
        head = int(rpc("eth_blockNumber", []), 16)
    except Exception as exc:  # noqa: BLE001 — an unread balance is not an empty one
        return tool._ar(error=f"owed({treasury}) on the sale {sale} could not be read ({exc}). "
                              f"Nothing was broadcast, and this is not evidence that nothing is owed.")
    header = (f"SALE CLAIM on {chain}: claim(treasury) on the pinned sale {sale}\n"
              f"  owed:     {_wei(owed)} to the treasury {treasury} (read from owed())\n")
    if owed <= 0:
        return tool._ar(content=header + "  RESULT: nothing is owed — no transaction.")
    intent = TxIntent(chain=chain, token=None, to=sale, amount_raw=0,
                      max_spend_usd=float(params.max_spend_usd), is_claim=True,
                      min_native_inflow_wei=owed)
    return await guarded_call(tool, execution_context=execution_context, verb="sale_claim",
                              intent=intent, inner_to=sale, inner_data=S.encode_claim(treasury),
                              dry_run=params.dry_run, rpc=rpc, header=header, hint=[owed, head],
                              fee_only=True)


async def settle(tool, params, execution_context):
    import asyncio
    from core.wallet import collection_mint
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    chain = params.chain
    rpc = tool.rpc_for(chain)
    try:
        treasury = _signer_address(tool)
        profile = pinned_sale(chain, params.collection)
        sale = profile.sale.address
        why = await asyncio.to_thread(collection_mint.runtime_refusal, chain, sale, rpc)
        if why:
            raise SaleError(why)
    except SaleError as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    try:
        settled = await asyncio.to_thread(S.read_settled, rpc, sale)
        if not settled:
            phase = _try(lambda: S.read_phase(rpc, sale))
            return tool._ar(content=(f"SALE SETTLE on {chain}: the sale {sale} is not settled "
                                     f"(phase {phase}) — settle() reverts NotSettled until then. "
                                     f"No transaction."))
        head = int(rpc("eth_blockNumber", []), 16)
        clearing = await asyncio.to_thread(S.read_clearing, rpc, sale)
        records = await asyncio.to_thread(own_open_records, rpc, profile, treasury, head=head,
                                          requested=params.records)
    except SaleError as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    except Exception as exc:  # noqa: BLE001 — an unread record is never skipped
        return tool._ar(error=f"the sale's records could not be read ({exc}). Nothing was "
                              f"broadcast, and this is not evidence that nothing is owed.")
    header = (f"SALE SETTLE on {chain}: settle(records) on the pinned sale {sale}\n"
              f"  clearing: {_wei(clearing)} per token (settled)\n")
    if not records:
        return tool._ar(content=header + "  records:  none of this treasury is open — no transaction.")
    more = records[S.MAX_SETTLE_RECORDS:]
    records = records[:S.MAX_SETTLE_RECORDS]
    refunds = [(i, max(0, (paid - clearing) * qty)) for i, qty, paid in records]
    total = sum(r for _i, r in refunds)
    header += (f"  records:  {[i for i, _r in refunds]} (this treasury's, open)"
               + (f"; {len(more)} more for the next call" if more else "") + "\n"
               f"  refund:   {_wei(total)} to the treasury (paidEach − clearing, per token)\n")
    if total <= 0:
        return tool._ar(content=header + (
            "  RESULT: these records refund nothing (they paid the clearing price). Their "
            "accounts' top-ups come with ANY settle of them (the project's settle job); a claim "
            "must receive something, so no transaction."))
    ids = [i for i, _r in refunds]
    intent = TxIntent(chain=chain, token=None, to=sale, amount_raw=0,
                      max_spend_usd=float(params.max_spend_usd), is_claim=True,
                      min_native_inflow_wei=total)
    return await guarded_call(tool, execution_context=execution_context, verb="sale_settle",
                              intent=intent, inner_to=sale, inner_data=S.encode_settle(ids),
                              dry_run=params.dry_run, rpc=rpc, header=header, hint=[total, head],
                              fee_only=True)
