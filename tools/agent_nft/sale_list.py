"""The seller's market orders of an agent NFT this treasury owns — core-owned (proposal 111 §8.1
``list`` (market) and ``accept``, §8.3 class O; 115 W9a; owner decisions 111 D8 and D9: one card
for each listing and each accept).

* ``agent_nft_sale_list`` — list ONE held NFT on its collection's PINNED market: the treasury
  SIGNS an EIP-712 ``Listing`` (signing class O, ``core.wallet.sign_policy.sign_market_order``:
  every field bound to a fact core reads itself, the ``sellerPromise`` built by core from a fresh
  read of the account). The market moves a sold token with ``transferFrom``, so the PINNED market
  first gets the single-token approval of that id (``approve(market, id)``, a guarded and
  measured act; never ``setApprovalForAll``). The result is the desk site's share link (``#x=``
  payload v1, ``k: "m"``) and a row in the open-order list (``core.wallet.market_orders``). A
  listing binds the token's ``transferCount``, not readiness: the agent keeps working while
  listed, but any account act that takes a holding below the promise makes the fill revert.
* ``agent_nft_sale_accept`` — accept a buyer's signed ``Offer`` (the desk's ``k: "o"`` link):
  the single-token approval of the market (when not there yet), then ``market.accept(offer, sig,
  id, buyerConditions, [])`` through the guard (``core/wallet/tx_guard_sale.py``): exactly that
  token out to the buyer, the ERC-20 ``price − royalty`` in, one ``Sold``, the approval consumed.

Both are ALWAYS owner-approved (an obligation to sell, an NFT that leaves), and the signature or
the transaction is made only on an owner turn or an owner's granted card (:func:`_owner_asked`).
Not built (named refusals): an ``Offer`` of the treasury's own, a Seaport listing (zone or plain).
"""
from __future__ import annotations

import secrets
from decimal import Decimal, InvalidOperation
from typing import Any

NOT_OWNER = ("a market order is the owner's act: it is signed only on the owner's own turn or "
             "after the owner approves its card (one card for each listing and each accept)")


class SaleOrderError(RuntimeError):
    """The verb cannot proceed; the text is the reason shown."""


def _owner_asked(tool, execution_context, max_spend_usd: float) -> bool:
    """True on a genuine owner turn or an owner's granted card covering this call (the guard's
    own step-0 answer, ``tx_guard.owner_authority``)."""
    import types
    from core.wallet import tx_guard
    from tools.controller.action_registration import _is_forged_or_autonomous_turn
    direct, granted = tx_guard.owner_authority(
        execution_context, _is_forged_or_autonomous_turn, tool,
        types.SimpleNamespace(max_spend_usd=float(max_spend_usd)))
    return bool(direct or granted)


def _units(text: str, decimals: int) -> int:
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        raise SaleOrderError(f"the price {text!r} is not a number") from None
    raw = value * (Decimal(10) ** int(decimals))
    if value <= 0 or raw != raw.to_integral_value():
        raise SaleOrderError(f"the price {text!r} is not above 0 in whole units of "
                             f"{decimals} decimals")
    return int(raw)


def _currency(rpc, chain: str, text: str):
    """``(address, symbol, decimals)``: native, or a token on core's reviewed list."""
    import core.wallet.sale_conditions as S
    t = str(text or "native").strip().lower()
    if t in ("native", "eth", S.ZERO):
        return S.ZERO, "ETH", 18
    reviewed = {x.lower() for x in S.read_context(chain).reviewed_erc20}
    if t not in reviewed:
        raise SaleOrderError(f"the currency {text} is neither native nor a reviewed token of "
                             f"{chain} ({', '.join(sorted(reviewed)) or 'none'})")
    dec = S._word_of(rpc("eth_call", [{"to": t, "data": S.DECIMALS_SELECTOR}, "latest"]))
    sym = S._symbol(rpc("eth_call", [{"to": t, "data": S.SYMBOL_SELECTOR}, "latest"]))
    return t, sym, int(dec)


def quote_listing(tool, params, held, treasury: str):
    """``(profile, typed, listing, promise, header, approved)`` of a listing; class O is checked
    on it before anything is shown as listable. Raises SaleOrderError."""
    import core.wallet.sale_acts as A
    import core.wallet.sale_conditions as S
    import core.wallet.sale_orders as O
    from core.wallet import collection_registry as registry
    from tools.agent_nft.sale_ready import SaleActError, pinned_market
    try:
        profile = pinned_market(held.chain, held.collection)
    except SaleActError as exc:
        raise SaleOrderError(str(exc)) from None
    std, rpc = profile.erc6551p, tool.rpc_for(held.chain)
    why = (registry.runtime_refusal(rpc, profile)
           or registry.code_pin_refusal(rpc, std.market, std.market_runtime_sha256, "pinned market"))
    if why:
        raise SaleOrderError(why)
    currency, symbol, decimals = _currency(rpc, held.chain, params.currency)
    price = _units(params.price, decimals)
    name, version, cid, verifying = O.read_domain(rpc, std.market)
    if verifying != std.market or cid != profile.chain_id:
        raise SaleOrderError("the pinned market's eip712Domain() names another contract or chain")
    now = int((rpc("eth_getBlockByNumber", ["latest", False]) or {}).get("timestamp"), 16)
    account, version_row = profile.account_of(held.token_id)
    ctx = S.read_context(held.chain)
    snap = S.read_snapshot(rpc, collection=profile.address, token_id=held.token_id,
                           account=account, deploy_block=profile.deploy_block, ctx=ctx)
    built = O.seller_promise(snap, ctx, params.tolerance_bps)
    listing = S.Listing(
        collection=profile.address, id=held.token_id,
        transferCount=S._word_of(rpc("eth_call", [{"to": profile.address, "data":
                                                   S.encode_transfer_count(held.token_id)},
                                                  "latest"])),
        seller=str(treasury).lower(), currency=currency, price=price,
        maxRoyalty=price * O.MAX_ROYALTY_BPS // 10_000, expiry=now + int(params.expiry_days) * 86_400,
        salt=secrets.randbits(128), promiseHash=S.conditions_hash(built.conditions),
        nonce=A.read_nonce(rpc, std.market, treasury))
    typed = O.typed_listing(listing, name=name, version=version, chain_id=cid, market=std.market)
    facts = O.read_listing_facts(rpc, registry, chain=held.chain, payload=typed, holder=treasury)
    cls, reason, _decoded, digest = O.classify_listing(typed, treasury, facts)
    if cls != "O":
        raise SaleOrderError(f"the listing is not signable (class {cls}): {reason}")
    approved = A.read_approved(rpc, profile.address, held.token_id)
    amount = Decimal(price) / (Decimal(10) ** decimals)
    header = (f"LIST {held.label} ({profile.address} #{held.token_id}) on {held.chain} — the "
              f"pinned market {std.market}\n"
              f"  price:    {amount.normalize():f} {symbol} ({price} raw), royalty at most "
              f"{listing.maxRoyalty} raw ({O.MAX_ROYALTY_BPS} bps) of it\n"
              f"  expires:  {listing.expiry} (chain time, {params.expiry_days} day(s)); salt "
              f"{listing.salt}; digest {digest[:18]}…\n"
              f"  what the buyer gets (the account {account}, {version_row.kind.label}, read now); "
              f"the promise checks {len(built.conditions)} of them at the fill (t = "
              f"{params.tolerance_bps} bps):\n"
              + "".join(f"            {ln}\n" for ln in S.card_lines(snap, built))
              + f"  binds:    transferCount {listing.transferCount} and nonce {listing.nonce}, NOT "
                f"readiness — I keep working while listed; any act through the account that takes "
                f"a promised holding below its minimum makes the fill revert (list again)\n"
              + f"  approval: the market {'IS' if approved == std.market else 'is NOT yet'} "
                f"approved for #{held.token_id} — "
              + ("no transaction" if approved == std.market else
                 "one transaction approve(market, id) first (one token, never setApprovalForAll)")
              + "; agent_nft_sale_cancel_listing cancels the listing and revokes it\n"
              + "  class O:  " + reason + "\n")
    return profile, typed, listing, built.conditions, header, approved


async def _approve_market(tool, execution_context, *, chain, collection, market, token_id,
                          max_spend_usd, rpc, header) -> Any:
    """The guarded single-token approval of the pinned market. Returns the ActionResult."""
    import core.wallet.sale_acts as A
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    intent = TxIntent(chain=chain, token=None, to=collection, amount_raw=0,
                      max_spend_usd=float(max_spend_usd), is_nft_op=True)
    return await guarded_call(tool, execution_context=execution_context, verb="sale_approve_market",
                              intent=intent, inner_to=collection,
                              inner_data=A.encode_approve(market, token_id), dry_run=False,
                              rpc=rpc, header=header, hint=[market, token_id],
                              asset=f"erc721:{collection}:{token_id}", counterparty=market,
                              fee_only=True)


def _landed(res) -> bool:
    return res.error is None and "(confirmed)" in (res.extracted_content or "")


async def list_(tool, params, execution_context) -> Any:
    import asyncio
    import time
    import core.wallet.sale_conditions as S
    import core.wallet.sale_orders as O
    from core.wallet import collection_registry as registry, market_orders
    from core.wallet.nft_account import NftAccountError
    from core.wallet.sign_policy import SignRefused, SignRequest, sign_market_order
    from tools.agent_nft.withdraw import _held
    try:
        held, signer = _held(tool, params, execution_context)
        profile, typed, listing, promise, header, approved = await asyncio.to_thread(
            quote_listing, tool, params, held, signer.address)
    except (NftAccountError, SaleOrderError, ValueError) as exc:
        return tool._ar(error=f"{exc}. Nothing was signed or broadcast.")
    std, rpc = profile.erc6551p, tool.rpc_for(held.chain)
    if params.dry_run:
        return tool._ar(content=header + "  RESULT: DRY RUN — nothing was signed or broadcast. "
                                         "Re-run with dry_run=false to list.")
    if not _owner_asked(tool, execution_context, params.max_spend_usd):
        return tool._ar(error=f"{NOT_OWNER}. Nothing was signed or broadcast.")
    # A listing the signer holds for the owner's on-box approval is signed AS IT WAS (the same
    # digest): a new quote would carry a new salt and expiry, which that approval does not cover.
    try:
        waiting = market_orders.held(profile.chain_id, profile.address, held.token_id,
                                     at=market_orders.path())
    except Exception as exc:  # noqa: BLE001 — unread is never "none waiting"
        return tool._ar(error=f"my market orders are unreadable ({exc}). Nothing was signed or "
                              f"broadcast.")
    if waiting is not None:
        old = S.Listing.of(waiting["typed"]["message"])
        if (old.price, old.currency) == (listing.price, listing.currency):
            typed, listing = waiting["typed"], old
            promise = [S.Condition.of(c) for c in waiting.get("promise") or []]
            header += (f"  retry:    the listing the signer holds for approval "
                       f"{waiting.get('approval_id')} (salt {old.salt}, expires {old.expiry}) — "
                       f"signed as it was\n")
    if approved != std.market:
        res = await _approve_market(tool, execution_context, chain=held.chain,
                                    collection=profile.address, market=std.market,
                                    token_id=held.token_id, max_spend_usd=params.max_spend_usd,
                                    rpc=rpc, header=header)
        if not _landed(res):
            return tool._ar(error=(res.error or res.extracted_content or "")
                            + "\n  The listing was NOT signed: the market's approval did not land.")
        header = res.extracted_content + "\n"
    try:
        facts = await asyncio.to_thread(O.read_listing_facts, rpc, registry, chain=held.chain,
                                        payload=typed, holder=signer.address)
        sig, verdict = await asyncio.to_thread(sign_market_order, signer,
                                               SignRequest(scheme="eip712", payload=typed),
                                               facts=facts, chain=held.chain)
    except SignRefused as exc:
        if waiting is not None:
            market_orders.release(profile.chain_id, profile.address, held.token_id,
                                  at=market_orders.path())
        return tool._ar(error=header + f"  RESULT: NOT SIGNED — {exc}")
    except Exception as exc:  # noqa: BLE001 — a signer failure signs nothing
        approval = _approval_id(exc)
        if approval:
            market_orders.hold({"chain_id": profile.chain_id, "collection": profile.address,
                                "token_id": held.token_id, "typed": typed,
                                "promise": [S.Condition.of(c).as_json() for c in promise],
                                "approval_id": approval}, at=market_orders.path())
            return tool._ar(error=header + (
                f"  RESULT: NOT SIGNED — the signer holds this listing for the owner's "
                f"independent approval {approval}. The owner approves it ON THE BOX: `sudo "
                f"polyrob owner promote signer_approval {approval}`; then run "
                f"agent_nft_sale_list again with the same price and currency: it signs this "
                f"same listing (one use). Reason: {getattr(exc, 'reason', exc)}"))
        return tool._ar(error=header + f"  RESULT: NOT SIGNED — the signer failed ({exc})")
    if waiting is not None:
        market_orders.release(profile.chain_id, profile.address, held.token_id,
                              at=market_orders.path())
    payload = O.listing_payload(chain_id=profile.chain_id, market=std.market, listing=listing,
                                sig=sig, promise=promise)
    link = "#x=" + S.encode_link(payload)
    row = {"chain": held.chain, "chain_id": profile.chain_id, "collection": profile.address,
           "token_id": held.token_id, "market": std.market, "seller": listing.seller,
           "salt": str(listing.salt), "nonce": listing.nonce,
           "transfer_count": listing.transferCount, "price": str(listing.price),
           "currency": listing.currency, "expiry": listing.expiry, "digest": verdict.digest,
           "signed_at": time.time(), "link": link}
    try:
        market_orders.record(row, at=market_orders.path())
        kept = "recorded in my open orders (/nft)"
    except Exception as exc:  # noqa: BLE001 — the signature exists: say it is not recorded
        kept = (f"⚠️ NOT recorded in my open orders ({exc}) — cancel it with "
                f"agent_nft_sale_cancel_listing salt={listing.salt} if it is not wanted")
    return tool._ar(content=header + f"  RESULT: LISTED — signed (class O), {kept}.\n"
                                     f"  share:    {link}\n"
                                     f"  (the desk site reads it after '#x='; agent_nft_buy reads "
                                     f"the same link)")


def _approval_id(exc) -> str:
    """The signer approval an ``approval_required`` refusal opened (the ``evm.send`` form:
    ``SignerRefused.extra['approval_id']``), else ``""``."""
    try:
        from core.signer import protocol
        from core.signer.client import SignerRefused
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(exc, SignerRefused) and exc.code == protocol.APPROVAL_REQUIRED:
        return str((exc.extra or {}).get("approval_id") or "")
    return ""


def _decode_offer(text: str, chain: str):
    import core.wallet.sale_conditions as S
    import core.wallet.sale_orders as O
    from core.wallet import collection_registry as registry
    try:
        payload = S.decode_link(text)
    except ValueError as exc:
        raise SaleOrderError(str(exc)) from None
    if payload["k"] != "o":
        raise SaleOrderError("this link is a listing, not an offer: a listing is bought "
                             "(agent_nft_buy), an offer is accepted")
    try:
        offer = O.Offer.of(payload["offer"])
        conds = [S.Condition.of(c) for c in payload.get("conditions") or []]
        sig = S._hex(payload["sig"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SaleOrderError(f"the offer is malformed ({exc})") from None
    if int(payload.get("chainId") or 0) != int(registry.chain_id_of(chain)):
        raise SaleOrderError(f"the offer is for chain {payload.get('chainId')}, not {chain}")
    return payload, offer, conds, sig


def quote_accept(tool, params, treasury: str):
    """``(profile, held, offer, data, header, approved)`` of an accept. Raises SaleOrderError."""
    import types
    import core.wallet.sale_acts as A
    import core.wallet.sale_conditions as S
    import core.wallet.sale_orders as O
    from tools.agent_nft.sale_ready import SaleActError, pinned_market
    from tools.agent_nft.withdraw import _held
    payload, offer, conds, sig = _decode_offer(params.offer, params.chain)
    try:
        profile = pinned_market(params.chain, offer.collection)
    except SaleActError as exc:
        raise SaleOrderError(str(exc)) from None
    std = profile.erc6551p
    if str(payload.get("market") or "").lower() != std.market:
        raise SaleOrderError(f"the offer names the market {payload.get('market')}, not the pinned "
                             f"{std.market}")
    if offer.anyId and not params.nft:
        raise SaleOrderError("the offer is for any id of the collection: name the NFT to sell (nft)")
    nft = params.nft or str(offer.id)
    held, _signer = _held(tool, types.SimpleNamespace(chain=params.chain,
                                                      nft=f"{offer.collection}#{nft.split('#')[-1]}",
                                                      account=None), None)
    if not offer.anyId and held.token_id != offer.id:
        raise SaleOrderError(f"the offer is for #{offer.id}, not #{held.token_id}")
    rpc = tool.rpc_for(params.chain)
    reviewed = {t.lower() for t in S.read_context(params.chain).reviewed_erc20}
    if offer.currency not in reviewed:
        raise SaleOrderError(f"the offer pays in {offer.currency}, not a reviewed token of "
                             f"{params.chain}")
    dec = S._word_of(rpc("eth_call", [{"to": offer.currency, "data": S.DECIMALS_SELECTOR}, "latest"]))
    sym = S._symbol(rpc("eth_call", [{"to": offer.currency, "data": S.SYMBOL_SELECTOR}, "latest"]))
    approved = A.read_approved(rpc, profile.address, held.token_id)
    data = O.encode_accept(offer, sig, held.token_id, conds, [])
    amount = Decimal(offer.price) / (Decimal(10) ** int(dec))
    header = (f"ACCEPT AN OFFER for {held.label} ({profile.address} #{held.token_id}) on "
              f"{params.chain} — the pinned market {std.market}\n"
              f"  buyer:    {offer.buyer}\n"
              f"  price:    {amount.normalize():f} {sym} ({offer.price} raw of {offer.currency}); "
              f"the royalty takes at most {offer.maxRoyalty} raw of it; the rest comes to me\n"
              f"  leaves:   the NFT and EVERYTHING its account {held.account} holds (the buyer's "
              f"{len(conds)} condition(s) are checked on it at the fill)\n"
              f"  expires:  {offer.expiry}; salt {offer.salt}\n"
              f"  approval: the market {'IS' if approved == std.market else 'is NOT yet'} approved "
              f"for #{held.token_id}"
              + ("" if approved == std.market else
                 " — one transaction approve(market, id) first (one token, never "
                 "setApprovalForAll); the accept consumes it") + "\n")
    return profile, held, offer, data, header, approved


async def accept(tool, params, execution_context) -> Any:
    import asyncio
    from core.wallet.nft_account import NftAccountError
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    try:
        wallet = tool._get_wallet()
        if wallet is None:
            from core.wallet.config import WALLET_OFF_TEXT
            raise SaleOrderError(WALLET_OFF_TEXT)
        treasury = wallet.operational_signer().address
        profile, held, offer, data, header, approved = await asyncio.to_thread(
            quote_accept, tool, params, treasury)
    except (NftAccountError, SaleOrderError, ValueError) as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    std, rpc = profile.erc6551p, tool.rpc_for(params.chain)
    if params.dry_run and approved != std.market:
        return tool._ar(content=header + "  RESULT: DRY RUN — the accept cannot be simulated "
                                         "before the market is approved; nothing was broadcast.")
    if not params.dry_run:
        if not _owner_asked(tool, execution_context, params.max_spend_usd):
            return tool._ar(error=f"{NOT_OWNER}. Nothing was broadcast.")
        if approved != std.market:
            res = await _approve_market(tool, execution_context, chain=params.chain,
                                        collection=profile.address, market=std.market,
                                        token_id=held.token_id,
                                        max_spend_usd=params.max_spend_usd, rpc=rpc, header=header)
            if not _landed(res):
                return tool._ar(error=(res.error or res.extracted_content or "")
                                + "\n  The offer was NOT accepted: the market's approval did not land.")
            header = res.extracted_content + "\n"
    intent = TxIntent(chain=params.chain, token=None, to=std.market, amount_raw=0,
                      max_spend_usd=float(params.max_spend_usd), is_nft_op=True,
                      nft_out=((profile.address, "erc721", held.token_id, 1),))
    return await guarded_call(tool, execution_context=execution_context, verb="sale_accept",
                              intent=intent, inner_to=std.market, inner_data=data,
                              dry_run=params.dry_run, rpc=rpc, header=header,
                              hint=[offer.buyer, offer.salt],
                              asset=f"erc721:{profile.address}:{held.token_id}",
                              counterparty=offer.buyer, fee_only=True)


async def revoke_market_approvals(tool, execution_context, *, chain: str, collection: str,
                                  market: str, token_ids, max_spend_usd: float, rpc) -> list:
    """After a cancel: ``approve(0, id)`` for each id whose ``getApproved`` is still the market
    (the guard's single-token revoke shape). Returns one line per id."""
    import asyncio
    import core.wallet.sale_acts as A
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    out = []
    for tid in sorted({int(t) for t in token_ids}):
        try:
            now = await asyncio.to_thread(A.read_approved, rpc, collection, tid)
        except Exception as exc:  # noqa: BLE001
            out.append(f"  approval #{tid}: unreadable ({exc}) — not revoked")
            continue
        if now != str(market).lower():
            continue
        intent = TxIntent(chain=chain, token=None, to=collection, amount_raw=0,
                          max_spend_usd=float(max_spend_usd), is_nft_op=True,
                          nft_approval_revokes=((collection, tid),))
        res = await guarded_call(tool, execution_context=execution_context,
                                 verb="sale_revoke_market", intent=intent, inner_to=collection,
                                 inner_data=A.encode_approve("0x" + "0" * 40, tid), dry_run=False,
                                 rpc=rpc, header=f"  revoke the market's approval of #{tid}\n",
                                 hint=[market, tid], counterparty=collection, fee_only=True)
        out.append(res.extracted_content if res.error is None else res.error)
    return out


def list_card_context(params: dict, *, tool=None) -> list:
    """The listing's card (D9: one per listing): price, currency, expiry, what the account holds
    (what the buyer gets) and what the listing binds. An unreadable quote is said ON the card."""
    import types
    if tool is None:
        from tools.agent_nft.tool import AgentNftTool
        tool = AgentNftTool()
    p = types.SimpleNamespace(chain=params.get("chain"), nft=params.get("nft"),
                              account=params.get("account"), price=params.get("price"),
                              currency=params.get("currency") or "native",
                              expiry_days=int(params.get("expiry_days") or 7),
                              tolerance_bps=int(params.get("tolerance_bps") or 0))
    try:
        from tools.agent_nft.withdraw import _held
        held, signer = _held(tool, p, None)
        header = quote_listing(tool, p, held, signer.address)[4]
    except Exception as exc:  # noqa: BLE001
        return [f"⚠️ THE LISTING COULD NOT BE READ ({type(exc).__name__}: {str(exc)[:200]}) — you "
                f"would approve without seeing the order or what the NFT's account holds."]
    return ["• The listing you approve (read now; signed once, valid until sold, cancelled, "
            "moved or expired):"] + ["    " + ln for ln in header.splitlines() if ln.strip()]


def accept_card_context(params: dict, *, tool=None) -> list:
    """The accept's card: the buyer, the price, what leaves with the NFT."""
    import types
    if tool is None:
        from tools.agent_nft.tool import AgentNftTool
        tool = AgentNftTool()
    p = types.SimpleNamespace(chain=params.get("chain"), offer=params.get("offer"),
                              nft=params.get("nft"))
    try:
        wallet = tool._get_wallet()
        header = quote_accept(tool, p, wallet.operational_signer().address)[4]
    except Exception as exc:  # noqa: BLE001
        return [f"⚠️ THE OFFER COULD NOT BE READ ({type(exc).__name__}: {str(exc)[:200]}) — you "
                f"would approve without seeing the buyer, the price or what leaves."]
    return ["• The offer you accept (read now):"] + [
        "    " + ln for ln in header.splitlines() if ln.strip()]


__all__ = ["SaleOrderError", "accept", "accept_card_context", "list_", "list_card_context",
           "quote_accept", "quote_listing", "revoke_market_approvals"]
