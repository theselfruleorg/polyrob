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
