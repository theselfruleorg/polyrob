"""RealX402Client — wraps the official x402 Python client.

API surface verified and recorded in docs/superpowers/plans/notes/x402-client-api.md.
Lazy-imports the SDK so the module imports cleanly even when x402 is absent
(the tool is gated OFF by default).

SDK sequence (x402 2.13.0):
  1. Build x402Client() — async payment-payload factory.
  2. Wrap signer: EthAccountSigner(signer.account) — exposes sign_typed_data + address.
  3. register_exact_evm_client(x402_client, sdk_signer) — registers eip155:* + V1 networks.
  4. async with wrapHttpxWithPayment(x402_client) as http — intercepts 402, pays, retries.
  5. http.request(method, url, content=body_bytes) — transparent auto-pay on 402.

quote() limitation: the x402 SDK has no price-only handshake without paying. quote()
performs a plain HEAD/GET probe and parses the 402 challenge header if the server
returns 402 immediately. This is best-effort: servers that don't return 402 on a
no-header request will return 200 and quote() returns None (free or no-402 endpoint).
The tool-side max_amount_usd guard is the operative cap; quote() is advisory only.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.wallet.signer import Signer
from tools.x402.client import X402Result

logger = logging.getLogger(__name__)


class RealX402Client:
    """Production x402 client adapter wrapping the official x402 Python SDK.

    Lazy-imports the SDK on first construction so this module is importable
    without `x402` installed (the x402 tool is default-OFF / gated).
    """

    def __init__(self) -> None:
        # Lazy import: verify the SDK is reachable at construction time so
        # callers get a clear ImportError rather than a late AttributeError.
        from x402 import x402Client  # noqa: F401 — verified present
        self._sdk_ready = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_sdk_signer(signer: Signer):
        """Adapt a POLYROB Signer to the SDK's EthAccountSigner wrapper.

        EthAccountSigner(account) wraps any eth_account.LocalAccount and
        exposes sign_typed_data + address (the ClientEvmSigner protocol).
        The raw account is accessed via signer.account — in-process only.
        """
        from x402.mechanisms.evm.signers import EthAccountSigner
        return EthAccountSigner(signer.account)  # type: ignore[attr-defined]

    @staticmethod
    def _parse_402_challenge(response) -> Optional[dict]:
        """Parse a 402 PAYMENT-REQUIRED challenge → {amount, network, pay_to}.

        Returns None if the header is absent. Individual fields may be None if not
        present/parseable. ``amount`` is normalised to USD (USDC 6-decimals assumed).
        """
        try:
            import base64
            import json

            header = (
                response.headers.get("PAYMENT-REQUIRED")
                or response.headers.get("payment-required")
                or response.headers.get("X-PAYMENT-REQUIRED")
            )
            if not header:
                return None
            decoded = json.loads(base64.b64decode(header + "=="))
            accepts = decoded.get("accepts", [])
            entry = accepts[0] if accepts else decoded  # V2 list, or V1 flat
            raw = entry.get("maxAmountRequired")
            amount = (float(raw) / 1_000_000) if raw is not None else None
            network = entry.get("network") or decoded.get("network")
            pay_to = entry.get("payTo") or entry.get("pay_to") or decoded.get("payTo")
            return {"amount": amount, "network": network, "pay_to": pay_to}
        except Exception as exc:  # noqa: BLE001
            logger.debug("quote: could not parse 402 header: %s", exc)
            # Distinguish "present but unparseable" from "absent": a present-but-broken
            # challenge must NOT be treated as free/None by the cap check (fail-closed).
            return {"amount": None, "network": None, "pay_to": None, "_unparseable": True}

    @classmethod
    def _parse_402_amount(cls, response) -> Optional[float]:
        """Best-effort: required USD amount from a 402 response (None if absent)."""
        ch = cls._parse_402_challenge(response)
        return ch.get("amount") if ch else None

    @staticmethod
    def _networks_match(configured: Optional[str], challenged: Optional[str]) -> bool:
        """Loose, case-insensitive network equivalence (names or eip155 ids)."""
        if not configured or not challenged:
            return True  # nothing to compare → don't block on absence
        a = str(configured).strip().lower()
        b = str(challenged).strip().lower()
        return a == b or a in b or b in a

    # ------------------------------------------------------------------
    # X402PaymentClient interface
    # ------------------------------------------------------------------

    async def quote(self, url: str) -> Optional[float]:
        """Best-effort price probe: return the required USD amount or None.

        Sends a plain GET without an X-PAYMENT header. If the server
        responds with 402, we parse the PAYMENT-REQUIRED challenge header to
        extract the amount. Returns None if the endpoint is free (200), the
        server doesn't honour no-header 402, or the header is unparseable.

        NOTE: The x402 SDK has no dedicated price-only handshake. This
        approach works for compliant servers but is advisory only — the
        max_amount_usd guard in fetch_with_payment is the operative cap.
        """
        try:
            import httpx
            async with httpx.AsyncClient() as http:
                resp = await http.get(url)
                if resp.status_code == 402:
                    return self._parse_402_amount(resp)
                return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("quote: probe failed for %s: %s", url, exc)
            return None

    async def fetch_with_payment(
        self,
        *,
        url: str,
        method: str,
        body: Optional[str],
        signer: Signer,
        network: str,
        facilitator_url: str,
        max_amount_usd: float,
    ) -> X402Result:
        """Perform an auto-paying HTTP request via the x402 SDK.

        SDK sequence:
          1. Build x402Client (payment-payload factory).
          2. Wrap signer → EthAccountSigner.
          3. register_exact_evm_client → registers eip155:* + all V1 networks.
          4. wrapHttpxWithPayment → AsyncClient that intercepts 402, pays, retries.
          5. Execute the request; map the response → X402Result.

        Max-amount guard: we parse the 402 challenge before paying and abort
        with a ValueError if the required amount exceeds max_amount_usd.
        """
        from x402 import x402Client
        from x402.mechanisms.evm.exact import register_exact_evm_client
        from x402.mechanisms.evm.signers import EthAccountSigner
        from x402.http.clients.httpx import wrapHttpxWithPayment

        sdk_signer = EthAccountSigner(signer.account)  # type: ignore[attr-defined]

        x402_c = x402Client()
        register_exact_evm_client(x402_c, sdk_signer)

        body_bytes = body.encode() if body else None

        # Single probe (NO retry/replay): intercept the 402 challenge to enforce the
        # cap + network BEFORE the SDK auto-pays, and capture the amount/pay_to here so
        # we never re-issue the request (esp. a non-idempotent POST) just to read it.
        probe_saw_402 = False
        probe_amount: Optional[float] = None
        probe_pay_to: Optional[str] = None
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient() as probe_client:
                probe_kwargs: dict = {"method": method, "url": url}
                if body_bytes:
                    probe_kwargs["content"] = body_bytes
                probe_resp = await probe_client.request(**probe_kwargs)
                if probe_resp.status_code == 402:
                    probe_saw_402 = True
                    challenge = self._parse_402_challenge(probe_resp) or {}
                    probe_amount = challenge.get("amount")
                    probe_pay_to = challenge.get("pay_to")
                    # H2 fail-CLOSED: if a payment is required but we can't read the
                    # amount, refuse rather than auto-pay an unknown sum.
                    if probe_amount is None:
                        raise ValueError(
                            f"x402: 402 challenge for {url} had no readable amount; "
                            f"refusing to auto-pay an unknown amount (fail-closed)"
                        )
                    if probe_amount > max_amount_usd:
                        raise ValueError(
                            f"x402: required amount ${probe_amount:.6f} exceeds "
                            f"max_amount_usd=${max_amount_usd:.6f} for {url}"
                        )
                    # H1: bind to the configured network — reject a challenge that
                    # demands a different chain than the operator configured.
                    challenged_net = challenge.get("network")
                    if not self._networks_match(network, challenged_net):
                        raise ValueError(
                            f"x402: challenge network '{challenged_net}' != configured "
                            f"'{network}' for {url}; refusing to pay (fail-closed)"
                        )
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            # A probe transport failure means we cannot verify the cap/network.
            # Fail-closed: do NOT proceed to a blind auto-pay.
            raise ValueError(
                f"x402: could not verify payment terms for {url} ({exc}); "
                f"refusing to auto-pay (fail-closed)"
            ) from exc

        # Auto-pay client: intercepts 402, signs + attaches X-PAYMENT, retries.
        async with wrapHttpxWithPayment(x402_c) as http:
            req_kwargs: dict = {"method": method, "url": url}
            if body_bytes:
                req_kwargs["content"] = body_bytes
            response = await http.request(**req_kwargs)

        # Recover payment metadata from response headers (best-effort).
        tx_hash: Optional[str] = (
            response.headers.get("X-PAYMENT-RESPONSE")
            or response.headers.get("x-payment-response")
        )
        pay_to: Optional[str] = (
            response.headers.get("X-PAY-TO")
            or response.headers.get("x-pay-to")
            or probe_pay_to
        )

        # C2: paid=True ONLY when we actually settled a 402 — the probe saw a 402 and
        # the SDK then returned a non-402 final response (which only happens via the
        # auto-pay retry). A plain 200 (free resource) has probe_saw_402=False, so it
        # no longer records a phantom $0.00 audit entry / "[paid $0.0000 to None]"
        # header. tx_hash is recorded when the server echoes it, but is NOT required
        # for `paid` (not all servers echo it — requiring it would under-record).
        paid = bool(probe_saw_402 and response.status_code != 402)
        amount_paid = probe_amount if paid else 0.0

        return X402Result(
            body=response.text,
            paid=paid,
            amount_usd=amount_paid or 0.0,
            tx_hash=tx_hash or None,
            pay_to=pay_to or None,
            status_code=response.status_code,
        )
