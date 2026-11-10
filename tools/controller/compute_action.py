"""The `compute` action — deterministic compute with no code exec (112 W4).

A server may run with ``CODE_EXEC_ENABLED=false``, so a hash or a draw must not need the
sandbox. This action is pure functions over ``core.fair_draw`` — no I/O, no network, no
sandbox — so it is on every seat.

* ``op="hash"`` — the hex digest of ``data`` (``text`` or ``hex``) under sha256, sha3_256
  (NIST) or keccak256 (Ethereum; needs the crypto extra, else a plain refusal).
* ``op="fair_draw"`` — sort ``ids`` by ``sha256(seed + id)`` and take ``n``: the SAME function
  any host script draws with (``core.fair_draw``), so a re-check is byte-for-byte.

⚠️ Registry-closure landmine: NO ``from __future__ import annotations`` in this module.
⚠️ Registered from ``tools/controller/service.py`` (``action_registration.py`` is at its
size ceiling) — the same hatch as ``room_read_action.py``.
"""
import logging
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from agents.task.agent.views import ActionResult

logger = logging.getLogger(__name__)


class ComputeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["hash", "fair_draw"] = Field(
        description="'hash' = a digest of data; 'fair_draw' = the provably fair draw.")
    algo: Literal["sha256", "sha3_256", "keccak256"] = Field(
        default="sha256",
        description="hash only. sha3_256 is NIST SHA-3; keccak256 is Ethereum's (they differ).")
    data: Optional[str] = Field(
        default=None, max_length=100_000, description="hash only: the input.")
    encoding: Literal["text", "hex"] = Field(
        default="text", description="hash only: read data as UTF-8 text or as hex bytes.")
    seed: Optional[str] = Field(
        default=None, max_length=200,
        description=("fair_draw only: the seed, used verbatim — for a block hash, the "
                     "0x-prefixed text the RPC returns."))
    ids: Optional[List[str]] = Field(
        default=None, description="fair_draw only: the unique entrant ids (at most 5000).")
    n: Optional[int] = Field(default=None, ge=0, description="fair_draw only: how many to draw.")


def register_compute_action(controller) -> None:
    """Register `compute` unconditionally: a pure function needs no gate."""

    @controller.registry.action(
        "Deterministic compute, no code exec: op='hash' gives the hex digest of data "
        "(sha256 | sha3_256 | keccak256; encoding text|hex); op='fair_draw' sorts ids by "
        "sha256(seed + id) and takes n — the one fair-draw rule (core.fair_draw). "
        "Use it instead of hashing by hand or asking a web service; never send a seed or "
        "entrant data to a third party.",
        param_model=ComputeAction,
    )
    async def compute(params: ComputeAction, execution_context=None) -> ActionResult:
        from core.fair_draw import ComputeError, fair_draw, hash_text
        try:
            if params.op == "hash":
                if params.data is None:
                    return ActionResult(error="compute hash: 'data' is required")
                digest = hash_text(params.algo, params.data, params.encoding)
                return ActionResult(extracted_content=f"{params.algo}({params.encoding}) = {digest}",
                                    include_in_memory=True)
            if not params.seed or params.ids is None or params.n is None:
                return ActionResult(error="compute fair_draw: 'seed', 'ids' and 'n' are required")
            winners = fair_draw(params.seed, params.ids, params.n)
            return ActionResult(
                extracted_content=(f"fair_draw: {len(winners)} of {len(params.ids)} by "
                                   f"sha256(seed + id), seed {params.seed!r}, in order: "
                                   + ", ".join(winners)),
                include_in_memory=True)
        except ComputeError as e:
            return ActionResult(error=f"compute {params.op}: {e}")
