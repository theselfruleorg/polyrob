"""CR-L21 (2026-09-23): the scoped `/pause trading` (trade_entry) binds an LP
deposit — it opens a position — while a removal or collection stays an exit."""
import pytest

from tests.unit.tools.defi import test_lp_verbs as lp
from tests.unit.tools.defi.test_lp_verbs import reads  # noqa: F401  (LP read stubs + flag)
from tools.defi.trade_tool import LpCollectParams, LpRemoveParams


@pytest.fixture
def entry_paused(monkeypatch):
    import core.autonomy_control as ac
    monkeypatch.setattr(ac, "pause_refusal_text",
                        lambda kind, what="": ("PAUSED: trading entries" if kind == "trade_entry" else None))


@pytest.mark.asyncio
async def test_lp_add_refuses_under_a_trade_entry_pause(entry_paused):
    t, gate = lp.tool()
    res = await t.lp_add(lp.add(dry_run=False))
    assert res.error and "PAUSED" in res.error
    assert not gate.recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("verb,params", [
    ("lp_remove", LpRemoveParams(chain="base", token_id=42)),
    ("lp_collect", LpCollectParams(chain="base", token_id=42))])
async def test_exits_still_run_under_a_trade_entry_pause(entry_paused, verb, params):
    t, gate = lp.tool()
    res = await getattr(t, verb)(params)
    assert res.error is None, res.error
    assert "DRY RUN" in (res.extracted_content or "")
