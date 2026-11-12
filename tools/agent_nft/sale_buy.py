"""The buyer's verbs of the checked sale — core-owned (proposal 111 §8.1 ``buy``, ``claim_owed``;
115 W9).

* ``agent_nft_buy`` — buy a MARKET listing of a pinned ERC-6551P collection: the treasury pays
  the listing's ETH price to the collection's pinned market, ``market.buy(listing, sig,
  sellerPromise, buyerConditions, recipient = the treasury)``. The listing is the link (or its
  code, or its JSON) the seller's site shares (the desk's ``#x=`` payload, ``k: "m"``). The
  buyer conditions are built from a FRESH read of the NFT's account
  (``core.wallet.sale_conditions``): every holding gets a condition the market checks AFTER the
  transfer, in the same transaction, so a seller who empties the account before the fill makes
  the fill revert and nothing is paid. The quote is simulated first; a holding off the reviewed
  list whose condition fails although the read shows it is dropped and labeled "cannot be
  checked, worth 0" (at most :data:`core.wallet.sale_conditions.MAX_DROPS`). ALWAYS
  owner-approved: it spends the price for an asset no cap can price.
* ``agent_nft_market_claim`` — ``market.claim(treasury)``: what the market owes this treasury
  (an ETH payment it refused at a fill). Sends nothing; the claim shape. Nothing owed = no
  transaction.

Not built here (named refusals): a Seaport listing (``checkedFill``), an ERC-20 price, an offer.
The guard is ``core/wallet/tx_guard_sale.py``: it re-reads the account and refuses a list it
would not build.
"""
from __future__ import annotations

import re
from typing import Any, Optional

NOT_SEAPORT = ("this is a Seaport listing: a checked fill (checkedFill) is not built in this "
               "wave — only the pinned market's own listings are bought")
NOT_OFFER = "this is an offer: its seller accepts it (not built in this wave)"


class BuyError(RuntimeError):
    """The verb cannot proceed; the text is the reason shown."""


def _treasury(tool) -> str:
    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        raise BuyError(WALLET_OFF_TEXT)
    return wallet.operational_signer().address


def _word(raw, i: int = 0) -> int:
    if not isinstance(raw, str) or len(raw) < 2 + 64 * (i + 1):
        raise ValueError(f"unreadable answer {str(raw)[:20]!r}")
    return int(raw[2 + 64 * i:2 + 64 * (i + 1)], 16)


def _revert_data(exc: Exception) -> Optional[str]:
    """The revert data an ``eth_call`` error carries (its ``data``), else None."""
    text = str(exc)
    m = re.search(r"['\"]data['\"]\s*:\s*['\"](0x[0-9a-fA-F]{8,})['\"]", text)
    if m:
        return m.group(1).lower()
    m = re.search(r"0x[0-9a-fA-F]{8}(?:[0-9a-fA-F]{64})*\b", text)
    return m.group(0).lower() if m else None


def _named(data: Optional[str]) -> str:
    from core.wallet.collection_errors import COLLECTION_ERRORS, SALE_RAIL_ERRORS
    from core.wallet.custom_errors import decode
    for who, table in (("the pinned market", SALE_RAIL_ERRORS),
                       ("the pinned collection", COLLECTION_ERRORS)):
        named = decode(data, table) if data else None
        if named:
            return f"{who} refuses it: {named}"
    return f"it reverts ({(data or 'no revert data')[:74]})"


def _facts(rpc, profile, listing, treasury: str, chain_id: int) -> dict:
    """The listing's live state, read before any quote (each read fails loudly)."""
    import core.wallet.abi as abi
    from core.wallet import sale_acts as A, sale_conditions as S
    market = profile.erc6551p.market

    def call(to, data):
        return rpc("eth_call", [{"to": to, "data": data}, "latest"])
    block = rpc("eth_getBlockByNumber", ["latest", False]) or {}
    out = {"now": int(block.get("timestamp"), 16),
           "owner": "0x" + format(_word(call(listing.collection,
                                             S.encode_owner_of(listing.id))), "064x")[-40:],
           "transfer_count": _word(call(listing.collection, S.encode_transfer_count(listing.id))),
           "nonce": A.read_nonce(rpc, market, listing.seller),
           "salt_used": A.read_used_salt(rpc, market, listing.seller, listing.salt),
           "seller_code": rpc("eth_getCode", [listing.seller, "latest"])}
    # ERC-5267: the domain is READ from the pinned market (never written in core), and core's
    # digest must equal the market's own listingDigest.
    _f, name, version, cid, verifying, _salt, _ext = abi.decode(
        [{"type": "bytes1"}, {"type": "string"}, {"type": "string"}, {"type": "uint256"},
         {"type": "address"}, {"type": "bytes32"}, {"type": "uint256[]"}],
        call(market, S.EIP712_DOMAIN_SELECTOR))
    if int(cid) != int(chain_id) or str(verifying).lower() != market:
        raise BuyError(f"the market's EIP-712 domain names chain {cid} and {verifying}, not "
                       f"{chain_id} and itself")
    mine = S.listing_digest(listing, name=name, version=version, chain_id=chain_id, market=market)
    theirs = "0x" + format(_word(call(market, S.encode_listing_digest(listing))), "064x")
    if mine != theirs:
        raise BuyError("core and the pinned market disagree on the listing's EIP-712 digest")
    out["digest"] = mine
    return out


def _dead(facts: dict, listing, treasury: str) -> list:
    from core.wallet.sale_acts import is_erc6551_clone
    why = []
    if facts["owner"] != listing.seller:
        why.append(f"the seller {listing.seller} does not own #{listing.id} now "
                   f"(its owner is {facts['owner']})")
    if facts["transfer_count"] != listing.transferCount:
        why.append("the NFT moved since the listing was signed (transferCount "
                   f"{listing.transferCount} → {facts['transfer_count']}): it does not work any more")
    if facts["now"] > listing.expiry:
        why.append(f"the listing expired (at {listing.expiry}, now {facts['now']})")
    if facts["salt_used"]:
        why.append("the seller cancelled this listing, or it was used (its salt is used)")
    if facts["nonce"] != listing.nonce:
        why.append("the seller cancelled every market listing (its nonce moved)")
    if is_erc6551_clone(facts["seller_code"]):
        why.append("the seller is an ERC-6551 account (the market refuses: SellerIsAccount)")
    if listing.seller == str(treasury).lower():
        why.append("this treasury is the seller")
    return why


def quote(tool, params, treasury: str):
    """``(profile, listing, data, header, snap, built)`` of a buy, simulated (the simulate-and-
    drop loop). Raises BuyError with the reason."""
    from core.wallet import collection_registry as registry, sale_conditions as S
    from tools.agent_nft.sale_ready import SaleActError, pinned_market
    try:
        payload = S.decode_link(params.listing)
    except ValueError as exc:
        raise BuyError(str(exc)) from None
    if payload["k"] == "s":
        raise BuyError(NOT_SEAPORT)
    if payload["k"] == "o":
        raise BuyError(NOT_OFFER)
    chain = params.chain
    chain_id = registry.chain_id_of(chain)
    try:
        listing = S.Listing.of(payload["listing"])
        promise = [S.Condition.of(c) for c in payload.get("promise") or []]
        sig = str(payload["sig"])
        profile = pinned_market(chain, listing.collection)
    except (KeyError, TypeError, ValueError) as exc:
        raise BuyError(f"the listing is malformed ({exc})") from None
    except SaleActError as exc:
        raise BuyError(str(exc)) from None
    std = profile.erc6551p
    if int(payload.get("chainId") or 0) != int(chain_id):
        raise BuyError(f"the listing is for chain {payload.get('chainId')}, not {chain} ({chain_id})")
    if str(payload.get("market") or "").lower() != std.market or listing.collection != profile.address:
        raise BuyError(f"the listing names another market or collection than the pinned "
                       f"{profile.address} and its market {std.market}")
    if listing.currency != S.ZERO:
        raise BuyError(f"the listing is priced in the token {listing.currency}: this build buys "
                       f"ETH listings only")
    if S.conditions_hash(promise) != listing.promiseHash:
        raise BuyError("the seller's promise in the link does not match what the seller signed")
    rpc = tool.rpc_for(chain)
    why = (registry.runtime_refusal(rpc, profile)
           or registry.code_pin_refusal(rpc, std.market, std.market_runtime_sha256, "pinned market"))
    if why:
        raise BuyError(why)
    try:
        facts = _facts(rpc, profile, listing, treasury, chain_id)
    except BuyError:
        raise
    except Exception as exc:  # noqa: BLE001 — unread is never "fine"
        raise BuyError(f"the listing's state could not be read ({exc})") from None
    dead = _dead(facts, listing, treasury)
    if dead:
        raise BuyError("this listing cannot be bought now: " + "; ".join(dead))
    account, version = profile.account_of(listing.id)
    if not version.kind.sale_ready_view:
        raise BuyError(f"#{listing.id} is held in {version.kind.label}: it has no sale rule")
    ctx = S.read_context(chain)
    exclude: dict = {}
    for _ in range(S.MAX_DROPS + 1):
        snap, built = S.read_and_build(rpc, collection=listing.collection, token_id=listing.id,
                                       account=account, deploy_block=profile.deploy_block,
                                       ctx=ctx, tolerance_bps=params.tolerance_bps,
                                       exclude=exclude)
        data = S.encode_buy(listing, sig, promise, built.conditions, treasury)
        try:
            rpc("eth_call", [{"from": treasury, "to": std.market, "data": data,
                              "value": hex(listing.price)}, "latest"])
            break
        except Exception as exc:  # noqa: BLE001 — a revert: name it, drop only what may be dropped
            rdata = _revert_data(exc)
            i = S.failed_condition_index(rdata)
            row = built.droppable().get(i - len(promise)) if i is not None else None
            if row is None or len(exclude) >= S.MAX_DROPS:
                raise BuyError(f"this would fail now: {_named(rdata)}. Nothing was sent") from None
            exclude[row.key] = S.DROPPED_WHY
    else:
        raise BuyError("the quote still fails after dropping what cannot be checked")
    chash = S.conditions_hash(built.conditions)
    want = str(getattr(params, "conditions_hash", None) or "").lower()
    if want and want != chash:
        raise BuyError(f"the NFT's account changed since the quote (its conditions hash was "
                       f"{want}, a fresh read builds {chash}) — quote again (dry_run=true) and "
                       f"check what it holds now")
    price_eth = listing.price / 10 ** 18
    header = (f"BUY {listing.collection} #{listing.id} on {chain} through the pinned market "
              f"{std.market}\n"
              f"  price:    {listing.price} wei ({price_eth:.6f} ETH) to the seller "
              f"{listing.seller} (royalty at most {listing.maxRoyalty} wei of it)\n"
              f"  account:  {account} ({version.kind.label}) — what you get with the NFT, read "
              f"now; {len(built.conditions)} condition(s) the market checks AFTER the transfer, "
              f"in the same transaction (a seller who empties it first makes the fill revert "
              f"and nothing is paid):\n"
              + "".join(f"            {ln}\n" for ln in S.card_lines(snap, built))
              + f"  listing:  expires {listing.expiry}, salt {listing.salt}, digest "
                f"{facts['digest'][:18]}…\n"
              + f"  quote:    conditions_hash {chash} — a real buy (dry_run=false) names it, "
                f"and refuses if the account changed since\n")
    return profile, listing, data, header, snap, built


async def buy(tool, params, execution_context) -> Any:
    import asyncio
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    try:
        treasury = _treasury(tool)
        if not params.dry_run and not params.conditions_hash:
            raise BuyError("a real buy (dry_run=false) names the conditions_hash its quote "
                           "printed: quote first with dry_run=true, then buy exactly that")
        profile, listing, data, header, _snap, _built = await asyncio.to_thread(
            quote, tool, params, treasury)
    except BuyError as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    std = profile.erc6551p
    intent = TxIntent(chain=params.chain, token=None, to=std.market, amount_raw=listing.price,
                      max_spend_usd=float(params.max_spend_usd),
                      expected_nft_in=((listing.collection, "erc721", listing.id, 1),))
    return await guarded_call(tool, execution_context=execution_context, verb="buy",
                              intent=intent, inner_to=std.market, inner_data=data,
                              inner_value=listing.price, dry_run=params.dry_run,
                              rpc=tool.rpc_for(params.chain), header=header,
                              hint=[listing.seller, listing.salt],
                              asset=f"erc721:{listing.collection}:{listing.id}",
                              counterparty=listing.seller)


async def claim(tool, params, execution_context) -> Any:
    import asyncio
    from core.wallet import collection_registry as registry, sale_conditions as S
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    from tools.agent_nft.sale_ready import SaleActError, pinned_market
    chain = params.chain
    rpc = tool.rpc_for(chain)
    try:
        treasury = _treasury(tool)
        profile = pinned_market(chain, params.collection)
        std = profile.erc6551p
        why = await asyncio.to_thread(registry.code_pin_refusal, rpc, std.market,
                                      std.market_runtime_sha256, "pinned market")
        if why:
            raise BuyError(why)
    except (BuyError, SaleActError) as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    try:
        owed = _word(await asyncio.to_thread(
            rpc, "eth_call", [{"to": std.market, "data": S.encode_owed(treasury)}, "latest"]))
    except Exception as exc:  # noqa: BLE001 — unread is not "nothing owed"
        return tool._ar(error=f"owed() on the market {std.market} could not be read ({exc}). "
                              f"Nothing was broadcast.")
    header = (f"CLAIM FROM THE MARKET on {chain}: claim({treasury}) on the pinned market "
              f"{std.market}\n  owed:     {owed} wei\n")
    if owed == 0:
        return tool._ar(content=header + "  RESULT: the market owes this treasury nothing — no "
                                         "transaction.")
    intent = TxIntent(chain=chain, token=None, to=std.market, amount_raw=0,
                      max_spend_usd=float(params.max_spend_usd), is_claim=True,
                      min_native_inflow_wei=owed)
    return await guarded_call(tool, execution_context=execution_context, verb="market_claim",
                              intent=intent, inner_to=std.market,
                              inner_data=S.encode_claim(treasury), dry_run=params.dry_run,
                              rpc=rpc, header=header, hint=[owed], counterparty=std.market,
                              fee_only=True)


def buy_card_context(params: dict, *, tool=None) -> list:
    """The approval card of ``agent_nft_buy``: what the buyer gets — the account's holdings read
    now, each checked or not, and the labeled gaps. An unreadable quote is said ON the card."""
    import types
    if tool is None:
        from tools.agent_nft.tool import AgentNftTool
        tool = AgentNftTool()
    p = types.SimpleNamespace(chain=params.get("chain"), listing=params.get("listing"),
                              tolerance_bps=int(params.get("tolerance_bps") or 0),
                              conditions_hash=params.get("conditions_hash"))
    try:
        _profile, listing, _data, header, _snap, _built = quote(tool, p, _treasury(tool))
    except Exception as exc:  # noqa: BLE001 — an unreadable quote is not an empty account
        return [f"⚠️ THE QUOTE COULD NOT BE READ ({type(exc).__name__}: {str(exc)[:200]}) — you "
                f"would approve without seeing what the NFT's account holds."]
    return ["• What you buy (read now; the market re-checks it at the fill):"] + [
        "    " + ln for ln in header.splitlines() if ln.strip()]


__all__ = ["BuyError", "buy", "buy_card_context", "claim", "quote"]
