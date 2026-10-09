"""Reserve an order at the SDK's HTTP boundary, after local validation/signing.

The per-call shallow copy keeps its transport hook out of cached clients and
concurrent requests. No signed order or authentication material enters the journal.
SDK transport retries are refused: an uncertain first send needs reconciliation.
"""
import copy


class OrderOutcomeUnknown(RuntimeError):
    def __init__(self, reference):
        self.reference = reference
        super().__init__(
            f"Order outcome unknown; reconcile {reference} before retrying"
        )


def submit_order(venue, client, holder, amount_usd, **order):
    """Build and send one order; return its result and durable submission reference.

    Called in a worker thread while the caller holds the policy reservation.
    Hyperliquid POST and Polymarket _post run after wire conversion, signatures
    and authentication headers are built. Errors before them cannot have sent
    the order, and therefore must not freeze the wallet.
    """
    from core.wallet.submission_journal import prepare_attempt, mark_rejected
    from polyrob_markets.order_safety import order_rejected

    if venue not in {"hyperliquid", "polymarket"}:
        raise ValueError("Unsupported order venue")
    isolated = copy.copy(client)
    if venue == "polymarket":
        isolated.retry_on_error = False
    transport_name = "post" if venue == "hyperliquid" else "_post"
    transport = getattr(isolated, transport_name)
    reference = None

    def send(*args, **kwargs):
        nonlocal reference
        if reference is not None:
            raise OrderOutcomeUnknown(reference)
        reference = prepare_attempt(venue, holder, amount_usd)
        return transport(*args, **kwargs)

    setattr(isolated, transport_name, send)
    try:
        if venue == "hyperliquid":
            if order.get("cloid") is not None:
                import re
                from hyperliquid.utils.types import Cloid
                if not isinstance(order["cloid"], str) or not re.fullmatch(r"0x[0-9a-fA-F]{32}", order["cloid"]):
                    raise ValueError("Client order ID must be a 16-byte hexadecimal value")
                order["cloid"] = Cloid.from_str(order["cloid"])
            result = isolated.order(**order)
        else:
            # Use the separate public methods: create_and_post_order can retry
            # after version changes. One attempted send has one reservation.
            signed = isolated.create_order(order["order_args"])
            result = isolated.post_order(signed)
        if reference is not None and order_rejected(venue, result):
            mark_rejected(reference, venue=venue)
    except Exception as exc:
        if reference is not None:
            raise OrderOutcomeUnknown(reference) from exc
        raise
    if reference is None:
        raise RuntimeError("Order SDK returned without reaching the guarded transport")
    return result, reference
