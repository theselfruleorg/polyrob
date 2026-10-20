"""Strict, quiet wire adapter for the pinned fastapi-x402 transport.

The dependency's settle parser ignores the body's success flag and its verify
method prints authorization headers. Neither method is used on the money path.
Its configured transport and CDP header builder remain the single auth source.
"""
import base64
import json
import re
from urllib.parse import urlsplit


class SettlementPending(RuntimeError):
    """Submission may have reached the chain; reconcile before another payment."""


class StrictFacilitator:
    def __init__(self, client):
        self._upstream = client

    async def _request(self, endpoint, payment_header, requirements):
        upstream = self._upstream
        url = getattr(upstream, f"{endpoint}_url")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.hostname not in {"api.cdp.coinbase.com", "x402.org", "www.x402.org"}
                or parsed.port not in (None, 443)):
            raise ValueError("untrusted facilitator endpoint")
        if len(payment_header) > 65536:
            raise ValueError("payment header too large")
        payment = json.loads(base64.b64decode(payment_header, validate=True))
        if not isinstance(payment, dict):
            raise ValueError("invalid payment payload")
        payload = {"paymentPayload": payment,
                   "paymentRequirements": requirements.model_dump(mode="json")}
        if upstream.is_coinbase_cdp:
            payload["x402Version"] = 1
        response = await upstream.client.post(
            url, json=payload, headers=upstream._create_coinbase_headers(endpoint),
            follow_redirects=False)
        if response.status_code != 200 or len(response.content) > 65536:
            raise ValueError("facilitator did not return a bounded successful response")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("invalid facilitator response")
        return data

    async def verify_and_settle_payment(self, payment_header, payment_requirements,
                                       *, before_settle=None):
        from fastapi_x402.models import SettleResponse, VerifyResponse

        data = await self._request("verify", payment_header, payment_requirements)
        if data.get("isValid") is not True:
            return (VerifyResponse(isValid=False, error="Payment verification failed"),
                    SettleResponse(success=False, errorReason="Verification failed"))
        verify = VerifyResponse(**data)
        if before_settle is not None:
            await before_settle()
        try:
            data = await self._request("settle", payment_header, payment_requirements)
        except Exception as exc:
            raise SettlementPending("Settlement outcome is unknown; reconciliation required") from exc
        tx = data.get("transaction")
        valid = (data.get("success") is True and isinstance(tx, str)
                 and re.fullmatch(r"0x[0-9a-fA-F]{64}", tx) is not None
                 and data.get("network") == payment_requirements.network)
        if not valid:
            if data.get("success") is False and not tx:
                # A definite refusal: the facilitator says it did not settle
                # and names no transaction. The caller may reopen the invoice.
                reason = data.get("errorReason")
                return verify, SettleResponse(
                    success=False,
                    errorReason=(reason[:200] if isinstance(reason, str) and reason
                                 else "Settlement rejected"))
            raise SettlementPending("Settlement is not proven; reconciliation required")
        return verify, SettleResponse(**data)


def get_facilitator_client():
    from fastapi_x402 import get_facilitator_client as upstream_client
    return StrictFacilitator(upstream_client())


#: ERC-20 ``Transfer(address,address,uint256)``.
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


#: Per-call RPC timeout and the whole-proof deadline for :func:`receipt_pays`
#: on the request path. Base mines a block about every 2 s, and the
#: facilitator answers after the tx is mined, so a real payment reads on the
#: first call; the deadline only bounds a slow or dead RPC.
RECEIPT_RPC_TIMEOUT_SEC = 3.0
RECEIPT_DEADLINE_SEC = 8.0


def receipt_pays(tx_hash, *, chain, token, pay_to, min_raw, call=None,
                 attempts=5, wait_sec=2.0, sleep=None, deadline_sec=None):
    """Does the facilitator's ``tx_hash`` really pay the treasury? (085 x402in L1)

    ``True`` only when the receipt on ``chain`` succeeded and carries a
    ``Transfer`` log from the pinned ``token`` contract to ``pay_to`` for at
    least ``min_raw``. ``False`` when the tx is mined and does not (reverted, or
    no such transfer). ``None`` when it cannot be read: no receipt after
    ``attempts`` polls, an RPC on the wrong chain, or a read failure.

    The facilitator is a pinned host, but its answer is still a third party's
    claim about our money; the receipt is the chain's own.
    """
    import time as _time

    from modules.x402.settlement_scan import _resolve_scan_target
    nap = sleep or _time.sleep
    stop_at = (None if deadline_sec is None
               else _time.monotonic() + float(deadline_sec))
    if call is None:
        target = _resolve_scan_target(chain)
        if target is None:
            return None
        url, expected = target
        from core.wallet.onchain import _rpc

        def call(method, params):
            return _rpc(url, method, params, RECEIPT_RPC_TIMEOUT_SEC)
        try:
            if int(str(call("eth_chainId", [])), 16) != int(expected):
                return None
        except Exception:
            return None
    receipt = None
    for i in range(max(1, int(attempts))):
        try:
            receipt = call("eth_getTransactionReceipt", [tx_hash])
        except Exception:
            receipt = None
        if isinstance(receipt, dict):
            break
        if stop_at is not None and _time.monotonic() + wait_sec >= stop_at:
            break
        if i + 1 < attempts:
            nap(wait_sec)
    if not isinstance(receipt, dict):
        return None
    if str(receipt.get("status") or "").lower() not in ("0x1", "1"):
        return False
    want_token = str(token or "").lower()
    want_to = str(pay_to or "").lower()[-40:]
    paid = 0
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if (str(log.get("address") or "").lower() != want_token or len(topics) != 3
                or str(topics[0]).lower() != _TRANSFER_TOPIC
                or str(topics[2]).lower()[-40:] != want_to):
            continue
        try:
            paid += int(str(log.get("data") or "0x0"), 16)
        except ValueError:
            return False
    return paid >= int(min_raw)


async def prove_settlement(row_id, tx_hash, *, chain, token, pay_to, min_raw):
    """The ONE request-path proof of a facilitator settlement (085 x402in L1),
    shared by `/pay` and the machine-payment middleware.

    Returns :func:`receipt_pays`'s answer, bounded by
    :data:`RECEIPT_DEADLINE_SEC`. On anything but ``True`` the facilitator tx
    is recorded on the held ``settling`` row (``row_id``), which is what lets
    the settlement watcher complete it later: the on-chain scan when it sees
    that tx pay the treasury, or the held-submission resolver's receipt read.
    """
    import asyncio
    import logging
    log = logging.getLogger(__name__)
    proven = await asyncio.to_thread(
        receipt_pays, tx_hash, chain=chain, token=token, pay_to=pay_to,
        min_raw=min_raw, deadline_sec=RECEIPT_DEADLINE_SEC)
    if proven is not True:
        log.warning("x402 %s: facilitator tx %s is %s on-chain; held for "
                    "reconciliation", row_id, tx_hash,
                    "NOT a payment to the treasury" if proven is False
                    else "unreadable")
        try:
            from modules.x402.settlement_holds import note_facilitator_tx
            if row_id:
                await note_facilitator_tx(row_id, tx_hash)
        except Exception:
            log.warning("x402 %s: could not record the facilitator tx", row_id,
                        exc_info=True)
    return proven
