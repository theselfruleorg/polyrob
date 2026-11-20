"""The core mint body of ``agent_nft_collection_mint`` (123 S3, as changed by 124 section 7).

The treasury mints ``qty`` tokens of a collection TO ITSELF through the collection's ``SALE()``
(the treasury then owns them and their accounts). Every fact is read from the chain now
(``core.wallet.collection_sale.read_sale``): the sale, its phase, its price, its per-call cap and
the next ids. No price, cap or chain is a constant of core. A pinned collection keeps its pin's
bounds (the guard's ``sale.max_price_wei``); any other collection is bounded by the owner's card
(the verb is always owner-approved). The guard shape is ``core/wallet/collection_mint.py``.
"""
from __future__ import annotations

from typing import Any, Optional


class MintError(RuntimeError):
    """The verb cannot proceed; the text is the reason shown."""


def _collection(chain: str, requested: Optional[str]) -> str:
    """The collection named, else the ONE pinned collection with a sale on *chain*."""
    if requested:
        return str(requested).lower()
    from core.wallet import collection_registry as registry
    try:
        pinned = [p for p in registry.profiles_on(registry.chain_id_of(chain)) if p.sale is not None]
    except Exception:  # noqa: BLE001 — an unreadable pin store is no pin
        pinned = []
    if len(pinned) == 1:
        return pinned[0].address
    raise MintError(f"name the collection to mint (`collection=<address>`) on {chain}")


async def mint(tool, params, execution_context) -> Any:
    from core.wallet import collection_mint, collection_sale
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call

    chain = params.chain
    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        return tool._ar(error=WALLET_OFF_TEXT)
    payer = wallet.operational_signer().address
    to = params.to or payer
    try:
        if str(to).lower() != str(payer).lower():
            raise MintError(f"a mint goes to the treasury ({payer}) only: the guard verifies the "
                            f"NFTs that ARRIVE in the paying wallet. Mint, then move a token with "
                            f"agent_nft_withdraw_token")
        collection = _collection(chain, params.collection)
        rpc = tool.rpc_for(chain)
        try:
            facts = collection_sale.read_sale(rpc, collection)
        except ValueError as exc:
            raise MintError(str(exc)) from exc
        _check_pin(chain, collection, facts.sale)
        qty = int(params.qty)
        if facts.max_per_tx and not 1 <= qty <= facts.max_per_tx:
            raise MintError(f"the sale mints 1..{facts.max_per_tx} per call; got {qty}")
        if not facts.mint_open or facts.phase == collection_sale.PHASE_CLOSED:
            raise MintError("the mint is not open (the sale has not started)")
        if qty > facts.public_capacity:
            raise MintError(f"sold out: {qty} is more than the {facts.public_capacity} the sale has left")
        ids = facts.ids_for(qty)
        value = facts.value_for(qty)
        min_out = int(params.min_out_per_token or 0)
        if facts.phase == collection_sale.PHASE_WHITELIST:
            if not params.allowance or params.proof is None:
                raise MintError("the sale is in its whitelist phase: pass the treasury's allowance "
                                "and proof (or wait for the public phase)")
            if collection_sale.read_wl_minted(rpc, facts.sale, payer) + qty > int(params.allowance):
                raise MintError(f"the whitelist allowance is {params.allowance}; this mint would pass it")
            data = collection_mint.encode_mint_wl(payer, qty, min_out, int(params.allowance),
                                                  list(params.proof))
        else:
            data = collection_mint.encode_mint(payer, qty, min_out)
    except MintError as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    intent = TxIntent(chain=chain, token=None, to=facts.sale, amount_raw=int(value),
                      max_spend_usd=params.max_spend_usd, idempotency_key="",
                      is_collection_mint=True, mint_ids=tuple(ids))
    phase = {1: "whitelist", 2: "auction", 3: "settled"}.get(facts.phase, str(facts.phase))
    header = (f"MINT {qty} (ids {ids[0]}..{ids[-1]}) of {collection} through its sale {facts.sale} "
              f"on {chain} (phase {phase}) to {payer}: {value} wei"
              + (f", at most {facts.price_now_wei + facts.step_wei} wei each now"
                 if facts.phase == collection_sale.PHASE_AUCTION else "")
              + ("; the excess above the clearing price comes back at settle"
                 if facts.phase == collection_sale.PHASE_AUCTION else "") + "\n")
    return await guarded_call(tool, execution_context=execution_context, verb="mint",
                              intent=intent, inner_to=facts.sale, inner_data=data,
                              inner_value=int(value), held=None, dry_run=params.dry_run,
                              header=header, rpc=rpc, hint=facts.total_minted,
                              counterparty=facts.sale)


def _check_pin(chain: str, collection: str, sale: str) -> None:
    """A pinned collection's ``SALE()`` must still be its pinned sale (124 section 5.2: a
    changed sale drops the pin's bounds; here it refuses, the owner re-pins)."""
    from core.wallet import collection_registry as registry
    try:
        p = registry.profile_for(registry.chain_id_of(chain), collection)
    except Exception:  # noqa: BLE001 — an unreadable pin store is no pin
        return
    if p is not None and p.sale is not None and p.sale.address != str(sale).lower():
        raise MintError(f"SALE() of {collection} is now {sale}, not the pinned sale "
                        f"{p.sale.address} — the owner re-pins it with /nft trust")


__all__ = ["MintError", "mint"]
