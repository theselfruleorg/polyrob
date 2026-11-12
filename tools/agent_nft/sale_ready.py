"""The seller's on-chain sale acts of an agent NFT this treasury owns — core-owned (115 W7;
111 §8.1 ``ready``, ``cancel_ready``, ``cancel_listing``).

* ``agent_nft_sale_ready`` — the treasury signs ``account.executeBatch([collection.readyToSell(id),
  JournalLog.log(entry)])`` (``account.execute(...)`` alone when no ``journal_log`` is pinned):
  the collection accepts ``readyToSell`` only from the token's OWN account, as the last leg but
  the journal. After it a PLAIN marketplace fill (an approved operator's ``transferFrom``) may
  move the NFT and everything its account holds, so it is ALWAYS owner-approved. While ready,
  the guard holds every automatic act through the account (each one ends readiness).
* ``agent_nft_sale_unready`` — the treasury calls ``collection.cancelReady(id)`` itself (the
  collection accepts the owner or the account; the account is not used, so its ``state()``
  stays). Moves nothing; readiness is false after.
* ``agent_nft_sale_cancel_listing`` — on the collection's pinned market (``standard.erc6551p.
  market``): ``cancel(salt)`` ends the one order signed with that salt; without a salt
  ``incrementNonce()`` ends EVERY order this treasury signed there. Moves nothing. Then (111
  W9a) the market's single-token approval of every id whose order it ended (the open-order list
  ``core.wallet.market_orders``, or the named ``nft``) is revoked, ``approve(0, id)``, one guarded
  call each.

Every write is a ``TxIntent`` with ``is_nft_op`` and nothing to move; ``tx_guard`` recognises the
act by its pinned target and calldata and measures it (``core/wallet/tx_guard_account.py``).
An unreadable value is named, never read as "not ready" or "nothing to cancel".
"""
from __future__ import annotations

from typing import Any, Optional

UNREADABLE = "unreadable"


class SaleActError(RuntimeError):
    """The verb cannot proceed; the text is the reason shown."""


def _kind_of(held):
    from core.wallet import collection_registry
    profile = collection_registry.profile_for(held.chain_id, held.collection)
    version = profile.version_for_account(held.token_id, held.account) if profile else None
    if version is None or not version.kind.sale_ready_view:
        label = version.kind.label if version is not None else "an unknown account kind"
        raise SaleActError(f"{held.label} is held in {label}: its collection has no sale "
                           f"readiness (no saleReadyOf view), so it cannot be made ready to sell")
    return profile, version.kind


def _ready_now(rpc, held):
    from core.wallet import sale_acts
    try:
        return sale_acts.read_sale_ready(rpc, held.collection, held.token_id)
    except Exception:  # noqa: BLE001 — unreadable is reported, never "not ready"
        return UNREADABLE


async def ready(tool, params, execution_context) -> Any:
    import asyncio
    from core.wallet import sale_acts
    from core.wallet.nft_account import NftAccountError
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    from tools.agent_nft.withdraw import _held
    try:
        held, signer = _held(tool, params, execution_context)
        _profile, kind = _kind_of(held)
    except (NftAccountError, SaleActError, ValueError) as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    rpc = tool.rpc_for(held.chain)
    now = await asyncio.to_thread(_ready_now, rpc, held)
    if now is True:
        return tool._ar(content=(f"READY FOR SALE {held.label}: it is already ready "
                                 f"(saleReadyOf is true) — no transaction."))
    header = (f"READY FOR SALE {held.label} ({held.collection} #{held.token_id}) on {held.chain}\n"
              f"  call:     readyToSell({held.token_id}) by its own account {held.account}"
              + (" + the journal entry, one batch" if held.journal_log else "") + "\n"
              f"  effect:   from the next block a PLAIN marketplace fill may move the NFT and "
              f"everything its account holds (no buyer check); a checked fill through the pinned "
              f"market does not need it\n"
              f"  hold:     while ready, every automatic act through the account is refused — any "
              f"act through it ends readiness; agent_nft_sale_unready ends it\n"
              f"  now:      saleReadyOf {now}\n")
    intent = TxIntent(chain=held.chain, token=None, to=held.collection, amount_raw=0,
                      max_spend_usd=float(params.max_spend_usd), is_nft_op=True)
    return await guarded_call(tool, execution_context=execution_context, verb="sale_ready",
                              intent=intent, inner_to=held.collection,
                              inner_data=sale_acts.encode_ready(held.token_id), held=held,
                              dry_run=params.dry_run, rpc=rpc, header=header,
                              asset=f"erc721:{held.collection}:{held.token_id}",
                              journal_kind="note",
                              journal_text=f"ready to sell {held.label} ({kind.label})",
                              fee_only=True)


async def unready(tool, params, execution_context) -> Any:
    import asyncio
    from core.wallet import sale_acts
    from core.wallet.nft_account import NftAccountError
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    from tools.agent_nft.withdraw import _held
    try:
        held, _signer = _held(tool, params, execution_context)
        _kind_of(held)
    except (NftAccountError, SaleActError, ValueError) as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    rpc = tool.rpc_for(held.chain)
    now = await asyncio.to_thread(_ready_now, rpc, held)
    try:   # the replay key: a later ready (an account batch) changes the account's state()
        from core.wallet import erc6551
        state = await asyncio.to_thread(erc6551.read_state, rpc, held.account)
    except Exception as exc:  # noqa: BLE001
        return tool._ar(error=f"the account {held.account} state() could not be read ({exc}). "
                              f"Nothing was broadcast.")
    header = (f"END SALE READINESS {held.label} ({held.collection} #{held.token_id}) on "
              f"{held.chain}\n"
              f"  call:     cancelReady({held.token_id}) from the treasury (the account is not "
              f"used)\n"
              f"  now:      saleReadyOf {now}\n")
    if now is False:
        return tool._ar(content=header + "  RESULT: it is not ready — no transaction.")
    intent = TxIntent(chain=held.chain, token=None, to=held.collection, amount_raw=0,
                      max_spend_usd=float(params.max_spend_usd), is_nft_op=True)
    return await guarded_call(tool, execution_context=execution_context, verb="sale_unready",
                              intent=intent, inner_to=held.collection,
                              inner_data=sale_acts.encode_cancel_ready(held.token_id),
                              dry_run=params.dry_run, rpc=rpc, header=header,
                              hint=[held.token_id, state],
                              asset=f"erc721:{held.collection}:{held.token_id}", fee_only=True)


def pinned_market(chain: str, requested: Optional[str] = None):
    """The pinned profile whose ``standard.erc6551p.market`` is set on *chain* (one, or the one
    named by *requested* — a collection or its market)."""
    from core.wallet import collection_registry as registry
    try:
        profiles = [p for p in registry.profiles_on(registry.chain_id_of(chain))
                    if p.erc6551p is not None]
    except Exception as exc:  # noqa: BLE001 — an untrusted registry pins nothing
        raise SaleActError(f"the owner's collection registry cannot be trusted ({exc})") from exc
    if requested:
        r = requested.lower()
        hit = next((p for p in profiles if r in (p.address, p.erc6551p.market)), None)
        if hit is None:
            raise SaleActError(f"{requested} is not a pinned collection (or its market) with a "
                               f"pinned market on {chain}")
        return hit
    if not profiles:
        raise SaleActError(f"no collection with a pinned market is on {chain} (the profile's "
                           f"standard.erc6551p.market)")
    if len(profiles) > 1:
        raise SaleActError(f"{len(profiles)} collections with a market are pinned on {chain}; "
                           f"name one (`collection`)")
    return profiles[0]


async def cancel_listing(tool, params, execution_context) -> Any:
    import asyncio
    from core.wallet import collection_registry as registry
    from core.wallet import sale_acts
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    chain = params.chain
    rpc = tool.rpc_for(chain)
    try:
        wallet = tool._get_wallet()
        if wallet is None:
            from core.wallet.config import WALLET_OFF_TEXT
            raise SaleActError(WALLET_OFF_TEXT)
        treasury = wallet.operational_signer().address
        profile = pinned_market(chain, params.collection)
        std = profile.erc6551p
        why = await asyncio.to_thread(registry.code_pin_refusal, rpc, std.market,
                                      std.market_runtime_sha256, "pinned market")
        if why:
            raise SaleActError(why)
    except SaleActError as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    market = std.market
    salt = params.salt
    try:
        if salt is not None:
            used = await asyncio.to_thread(sale_acts.read_used_salt, rpc, market, treasury, salt)
            nonce = None
        else:
            used = None
            nonce = await asyncio.to_thread(sale_acts.read_nonce, rpc, market, treasury)
    except Exception as exc:  # noqa: BLE001 — unread is not "nothing to cancel"
        return tool._ar(error=f"the market {market} could not be read ({exc}). Nothing was "
                              f"broadcast.")
    if salt is not None:
        header = (f"CANCEL MARKET ORDER on {chain}: cancel({salt}) on the pinned market {market}\n"
                  f"  ends:     the one order this treasury {treasury} signed with salt {salt}\n")
        if used:
            return tool._ar(content=header + "  RESULT: that salt is already used or cancelled "
                                             "— no transaction.")
        data = sale_acts.encode_market_cancel(salt)
    else:
        header = (f"CANCEL ALL MARKET ORDERS on {chain}: incrementNonce() on the pinned market "
                  f"{market}\n"
                  f"  ends:     EVERY listing and offer this treasury {treasury} signed there "
                  f"(nonce {nonce} → {nonce + 1})\n")
        data = sale_acts.encode_increment_nonce()
    intent = TxIntent(chain=chain, token=None, to=market, amount_raw=0,
                      max_spend_usd=float(params.max_spend_usd), is_nft_op=True)
    res = await guarded_call(tool, execution_context=execution_context,
                             verb="sale_cancel_listing", intent=intent, inner_to=market,
                             inner_data=data, dry_run=params.dry_run, rpc=rpc, header=header,
                             hint=[salt, nonce], counterparty=market, fee_only=True)
    if params.dry_run or res.error is not None or "(confirmed)" not in (res.extracted_content or ""):
        return res
    ids = _ended_ids(profile, salt, getattr(params, "nft", None))
    if not ids:
        return res
    from tools.agent_nft.sale_list import revoke_market_approvals
    lines = await revoke_market_approvals(tool, execution_context, chain=chain,
                                          collection=profile.address, market=market,
                                          token_ids=ids, max_spend_usd=params.max_spend_usd,
                                          rpc=rpc)
    return tool._ar(content=res.extracted_content + "\n" + "\n".join(
        lines or ["  approvals: the market holds no approval of those ids — nothing to revoke"]))


def _ended_ids(profile, salt, nft) -> list:
    """The ids whose market approval a cancel leaves without an order: the named ``nft``, else the
    open orders this cancel ended (one salt, or every order on the market)."""
    if nft:
        return [int(str(nft).split("#")[-1])]
    try:
        from core.wallet import market_orders
        rows = market_orders.open_orders(at=market_orders.path())
    except Exception:  # noqa: BLE001 — unread: nothing revoked here, said by the empty list
        return []
    return [int(r["token_id"]) for r in rows
            if str(r.get("market")) == profile.erc6551p.market
            and str(r.get("collection")) == profile.address
            and (salt is None or str(r.get("salt")) == str(salt))]
