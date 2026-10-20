"""CR-M07: a defi_data read that DEFAULTS to the operator wallet is an owner
read, and reconcile reads the caller's own book only."""
import pytest

import core.wallet.factory as wf
from tools.defi.data_tool import (DefiDataTool, LpPositionsParams,
                                  NftHoldingsParams, PortfolioParams,
                                  ReconcileParams)


class _Ctx:
    def __init__(self, user_id, workspace_dir=None):
        self.user_id = user_id
        self.workspace_dir = workspace_dir


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    reads = []

    class _W:
        address = "0x" + "11" * 20
        solana_address = "So1anaOperator"

        def operational_signer(self):
            return self

    monkeypatch.setattr(wf, "get_agent_wallet", lambda: reads.append(1) or _W())
    return reads


STRANGER = _Ctx("u_stranger")


@pytest.mark.asyncio
@pytest.mark.parametrize("chain", ["base", "solana"])
async def test_portfolio_default_wallet_refuses_a_non_owner(_owner, chain):
    res = await DefiDataTool().portfolio(PortfolioParams(chain=chain),
                                         execution_context=STRANGER)
    assert res.error and "owner" in res.error
    assert _owner == []


@pytest.mark.asyncio
async def test_nft_and_lp_defaults_refuse_a_non_owner(_owner):
    tool = DefiDataTool()
    res = await tool.nft_holdings(NftHoldingsParams(), execution_context=STRANGER)
    assert res.error and "owner" in res.error
    res = await tool.lp_positions(LpPositionsParams(), execution_context=STRANGER)
    assert res.error and "owner" in res.error
    assert _owner == []


@pytest.mark.asyncio
async def test_reconcile_default_wallet_refuses_a_non_owner(_owner, tmp_path):
    res = await DefiDataTool().reconcile(ReconcileParams(chain="base"),
                                         execution_context=STRANGER)
    assert res.error and "owner" in res.error
    assert _owner == []


@pytest.mark.asyncio
async def test_reconcile_reads_only_the_callers_own_book(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from core.open_positions import PositionDelta, apply_delta, open_positions_db_path
    apply_delta("u_victim", PositionDelta("base", "0x" + "ab" * 20, "V", 5.0, 1.0),
                db_path=open_positions_db_path())
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {})
    res = await tool.reconcile(ReconcileParams(chain="base"),
                               execution_context=_Ctx("u_stranger"))
    assert res.error is None, res.error
    assert res.metadata["report"]["rail_rows"] == 0
