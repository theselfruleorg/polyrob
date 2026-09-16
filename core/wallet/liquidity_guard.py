"""Liquidity-specific assertions, called only by the shared tx authorizer.

The pinned v3 manager enforces that burn follows a complete decrease/collect.
v4 is refused until its Permit2 measurement path is implemented.
"""
from math import isfinite

from core.wallet import dex_registry

ZERO = "0x" + "0" * 40


def legs(items):
    return {(token.lower() if token else None): amount for token, amount in items}


def structural(intent, tx):
    if any((intent.is_deploy, intent.is_claim, intent.is_registration,
            intent.is_allowance_op, intent.is_nft_op)):
        raise ValueError("liquidity cannot be combined with another intent shape")
    if (intent.token or intent.amount_raw or intent.nft_out or intent.expected_nft_in
            or intent.expected_allowance_grants or intent.nft_operator_ops
            or intent.inflow_token or intent.min_native_inflow_wei is not None
            or intent.min_inflow_raw is not None):
        raise ValueError("liquidity must declare its effects only in LP fields")
    row = dex_registry.row_for(intent.chain, "v3")
    npm = dex_registry.resolve_position_manager(intent.chain, "v3").lower()
    if (str(intent.to).lower() != npm or str(tx.get("to")).lower() != npm):
        raise ValueError("liquidity transaction and intent must target the pinned v3 position manager")
    if tx.get("chainId") is not None and int(tx["chainId"]) != row.chain_id:
        raise ValueError("liquidity transaction chainId does not match the pinned chain")
    if not (intent.lp_outflows or intent.lp_inflows or intent.expected_events):
        raise ValueError("liquidity must assert an outflow, inflow, or required event")
    for values, minimum in ((intent.lp_outflows, 1), (intent.lp_inflows, 0)):
        if len(values) > 2 or len(legs(values)) != len(values):
            raise ValueError("liquidity requires at most two distinct legs per direction")
        if any(not isinstance(n, int) or isinstance(n, bool) or n < minimum for _, n in values):
            raise ValueError("invalid liquidity leg amount")
    if legs(intent.lp_outflows).keys() & legs(intent.lp_inflows).keys():
        raise ValueError("a liquidity leg cannot declare both directions")
    native_max = legs(intent.lp_outflows).get(None, 0)
    if int(tx.get("value") or 0) != native_max:
        raise ValueError("native liquidity leg must equal tx.value")
    if not intent.lp_position or intent.lp_position[0].lower() != npm:
        raise ValueError("liquidity must declare the pinned position manager")
    token_id = intent.lp_position[1]
    effect = intent.lp_position_effect
    if effect not in ("mint", "hold", "burn"):
        raise ValueError("unknown liquidity position effect")
    if effect == "mint":
        if token_id is not None or not intent.lp_outflows:
            raise ValueError("mint requires an unknown id and a deposit")
    elif not isinstance(token_id, int) or isinstance(token_id, bool) or token_id < 0:
        raise ValueError("hold/burn requires a position id")
    if effect == "burn" and (intent.lp_outflows or not any(n > 0 for _, n in intent.lp_inflows)):
        raise ValueError("burn requires a withdrawal with positive receipts; standalone burn is unsupported")
    for emitter, topic in intent.expected_events:
        if emitter.lower() not in (npm, row.factory.lower()):
            raise ValueError("required event emitter is not a pinned liquidity contract")
        if len(topic) != 66 or not topic.startswith("0x"):
            raise ValueError("required event topic must be bytes32")
        bytes.fromhex(topic[2:])


def assert_deltas(intent, deltas, dust):
    outgoing, incoming = legs(intent.lp_outflows), legs(intent.lp_inflows)
    measured = {t.lower(): n for t, n in deltas.token_deltas.items()}
    measured[None] = deltas.native_delta
    for token, maximum in outgoing.items():
        moved = measured.get(token)
        if moved is None or not 0 < -moved <= maximum:
            raise ValueError(f"liquidity outflow {token}: measured {moved}, expected 0 < outflow <= {maximum}")
    for token, minimum in incoming.items():
        moved = measured.get(token)
        if moved is None or moved < minimum:
            raise ValueError(f"liquidity inflow {token}: measured {moved}, below minimum {minimum}")
    for token, moved in measured.items():
        if token not in outgoing and token not in incoming and abs(moved) > (dust if token is None else 0):
            raise ValueError(f"UNDECLARED liquidity token delta: {token} {moved}")
    npm, token_id = intent.lp_position
    effect = intent.lp_position_effect
    ins, outs = deltas.holder_nft_in, deltas.holder_nft_out
    minted_id = None
    if effect == "mint":
        if (len(ins) != 1 or outs or ins[0][0].lower() != npm.lower()
                or ins[0][1] != "erc721" or ins[0][2].lower() != ZERO or ins[0][4] != 1):
            raise ValueError("liquidity mint must mint exactly ONE position NFT from the pinned manager")
        minted_id = int(ins[0][3])
    elif effect == "hold":
        if ins or outs:
            raise ValueError("liquidity hold must not transfer any NFT")
    elif (ins or len(outs) != 1 or
          tuple(outs[0]) != (npm.lower(), "erc721", ZERO, token_id, 1)):
        raise ValueError("liquidity burn must destroy exactly the declared position NFT")
    observed = {(a.lower(), t.lower()) for a, t in deltas.event_topics}
    for emitter, topic in intent.expected_events:
        if (emitter.lower(), topic.lower()) not in observed:
            raise ValueError(f"required event {topic} was not emitted by {emitter}")
    assert_position_events(intent, deltas.logs, minted_id if effect == "mint" else token_id)
    return minted_id


def assert_position_events(intent, logs, token_id):
    """A pinned event about SOMEBODY ELSE's position is not our receipt."""
    from eth_utils import keccak
    signatures = {
        "increase": "IncreaseLiquidity(uint256,uint128,uint256,uint256)",
        "decrease": "DecreaseLiquidity(uint256,uint128,uint256,uint256)",
        "collect": "Collect(uint256,address,uint256,uint256)",
    }
    topics = {"0x" + keccak(text=sig).hex(): name for name, sig in signatures.items()}
    seen = set()
    for log in logs:
        ts = log.get("topics", [])
        if str(log.get("address", "")).lower() != intent.to.lower() or not ts:
            continue
        kind = topics.get(str(ts[0]).lower())
        if kind is None:
            continue
        if len(ts) != 2 or int(ts[1], 16) != token_id:
            raise ValueError("liquidity event concerns a different position id")
        raw = bytes.fromhex(log.get("data", "")[2:])
        if len(raw) != 96:
            raise ValueError("malformed liquidity position event")
        if kind in ("increase", "decrease") and int.from_bytes(raw[:32], "big") <= 0:
            raise ValueError("liquidity position event reports no liquidity change")
        seen.add(kind)
    required = "increase" if intent.lp_outflows else "collect" if intent.lp_inflows else "decrease"
    if required not in seen:
        raise ValueError(f"missing {required} event for the declared position id")
    if intent.lp_outflows and ("decrease" in seen or "collect" in seen):
        raise ValueError("a deposit may not also withdraw liquidity")


def price_outflows(intent, deltas, price_fn, fallback_price_fn, decimals_fn):
    from core.wallet import chains
    row = chains.get(intent.chain)
    held = legs(intent.lp_held_balances)
    measured = {t.lower(): n for t, n in deltas.token_deltas.items()}
    measured[None] = deltas.native_delta
    values = []
    for token, _maximum in intent.lp_outflows:
        key = token.lower() if token else None
        raw = -measured[key]
        asset = token or row.wrapped_native
        def quoted(fn):
            try:
                p = fn(intent.chain, asset) if fn else None
                return p if p is not None and isfinite(p) and p > 0 else None
            except Exception:
                return None
        price = quoted(price_fn)
        if price is None and raw <= held.get(key, -1):
            price = quoted(fallback_price_fn)
        decimals = decimals_fn(intent.chain, token) if token else row.native_decimals
        if decimals is None:
            raise ValueError("liquidity token decimals unknown")
        values.append(None if price is None else raw / 10 ** decimals * price)
    implied = any(v is None for v in values)
    if implied:
        # Only two-sided deposits permit this conservative paired-leg valuation.
        # Both positive outflows were independently measured above.
        known = [v for v in values if v is not None]
        if len(values) != 2 or len(known) != 1:
            raise ValueError("unpriceable liquidity outflow; booking it at $0.00 would widen every other cap")
        values = [known[0] if v is None else v for v in values]
    return sum(values), implied
