"""Conservative order valuation and explicit venue acceptance checks."""
import math


def order_notional(size, price, reference):
    values = tuple(float(v) for v in (size, price, reference))
    if any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("A finite positive live price and order size are required")
    size, price, reference = values
    if abs(price / reference - 1) > 0.200001:
        raise ValueError("Limit price is outside the 20% live reference price band")
    amount = size * max(price, reference)
    if not math.isfinite(amount):
        raise ValueError("Order notional is not finite")
    return amount


def order_accepted(venue, result):
    if not isinstance(result, dict) or result.get("success") is False or result.get("error"):
        return False
    if venue == "polymarket":
        return (result.get('success') is True and isinstance(result.get('orderID'), str)
                and bool(result['orderID']) and not result.get('errorMsg')
                and result.get('status') in {'live', 'matched', 'delayed', 'unmatched'})
    if result.get("status") != "ok":
        return False
    response = result.get("response")
    if not isinstance(response, dict):
        return False
    data = response.get('data')
    statuses = data.get('statuses') if isinstance(data, dict) else None
    return isinstance(statuses, list) and len(statuses) == 1 and all(isinstance(row, dict) and
                                 ("resting" in row or "filled" in row) and
                                 not row.get("error") for row in statuses)


def order_rejected(venue, result):
    """Only explicit rejection of this single order proves there was no fill."""
    if not isinstance(result, dict):
        return False
    if venue == 'polymarket':
        return (result.get('success') is False and not result.get('orderID')
                and result.get('status') in (None, '', 'failed', 'rejected', 'error')
                and isinstance(result.get('errorMsg'), str) and bool(result['errorMsg']))
    if venue != 'hyperliquid':
        return False
    if result.get('status') == 'err':
        return isinstance(result.get('response'), str) and bool(result['response'])
    response = result.get('response')
    if result.get('status') != 'ok' or not isinstance(response, dict) or response.get('type') != 'order':
        return False
    data = response.get('data')
    statuses = data.get('statuses') if isinstance(data, dict) else None
    return (isinstance(statuses, list) and len(statuses) == 1
            and isinstance(statuses[0], dict) and set(statuses[0]) == {'error'}
            and isinstance(statuses[0]['error'], str) and bool(statuses[0]['error']))


def order_refusal(venue, result, reference):
    if order_rejected(venue, result):
        return 'Venue rejected the order; no fill was booked and its submission reservation was released'
    return f'Order outcome unknown; reconcile {reference} before retrying'
