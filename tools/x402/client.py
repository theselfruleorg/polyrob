"""x402 client boundary. The tool depends on X402PaymentClient (an interface),
so the official x402 SDK (RealX402Client, Task 8) is swappable and tests use a
deterministic FakeX402Client. The signer is the agent wallet's x402 signer —
the raw key never leaves it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from core.wallet.signer import Signer


@dataclass(frozen=True)
class X402Result:
    body: str
    paid: bool
    amount_usd: float
    tx_hash: Optional[str]
    pay_to: Optional[str]
    status_code: int


class X402PaymentClient(Protocol):
    async def quote(self, url: str) -> Optional[float]: ...
    async def fetch_with_payment(
        self, *, url: str, method: str, body: Optional[str], signer: Signer,
        network: str, facilitator_url: str, max_amount_usd: float,
    ) -> X402Result: ...


class FakeX402Client:
    """Deterministic test double."""

    def __init__(self, price_usd: Optional[float], pay_to: Optional[str], paid_body: str):
        self._price = price_usd
        self._pay_to = pay_to
        self._body = paid_body

    async def quote(self, url: str) -> Optional[float]:
        return self._price

    async def fetch_with_payment(self, *, url, method, body, signer, network, facilitator_url, max_amount_usd) -> X402Result:
        if self._price is None:
            return X402Result(body=self._body, paid=False, amount_usd=0.0, tx_hash=None, pay_to=None, status_code=200)
        return X402Result(body=self._body, paid=True, amount_usd=self._price,
                          tx_hash="0xfake", pay_to=self._pay_to, status_code=200)
