"""``defi_data.retire_row`` — the agent's retire of a gone/dust book row (086 W0).

Mixed into :class:`tools.defi.data_tool.DefiDataTool`. The verb takes NO
balance: it measures the operator wallet's balance itself (``tools/defi/
retire.py``, the reconcile seams) and the core refuses anything but a measured
zero or dust (``core.position_retire``). An owner read of the operator wallet,
so it carries the same owner probe as ``reconcile`` (CR-M07).
"""
from __future__ import annotations  # safe: @BaseTool.action uses explicit param_model

import asyncio

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool


class RetireRowParams(BaseModel):
    chain: str = Field(description="The chain the book row is on (base, robinhood, solana, …)")
    address: str = Field(description="The token contract / mint address of the book row")


class RetireReadsMixin:
    """The one book write ``defi_data`` makes: closing a row the chain shows is
    gone. Nothing is signed or broadcast."""

    @BaseTool.action(
        "Close (retire) a position-book row whose tokens are GONE or DUST "
        "(worth under $1). Measures the wallet's balance on chain itself and "
        "refuses unless it is zero or dust; the measurement is stored as proof. "
        "Moves nothing. A holding worth more is sold, or the owner writes it off.",
        param_model=RetireRowParams)
    async def retire_row(self, params: RetireRowParams, execution_context=None):
        return await asyncio.to_thread(self._retire_row_sync, params, execution_context)

    def _retire_row_sync(self, params: RetireRowParams, execution_context=None):
        from tools.defi.data_tool import _operator_read_refusal  # call-time
        refusal = _operator_read_refusal(execution_context)
        if self._holder == "__from_wallet__" and refusal:
            return self._ar(error=refusal)
        uid = self._rail_uid(execution_context)
        from tools.defi.retire import retire_with_proof
        ok, text = retire_with_proof(uid, params.chain, params.address, execute=True,
                                     tool=self)
        if not ok:
            return self._ar(error=text)
        return self._ar(content=text, metadata={"retired": True, "chain": params.chain,
                                                "address": params.address})


__all__ = ["RetireRowParams", "RetireReadsMixin"]
