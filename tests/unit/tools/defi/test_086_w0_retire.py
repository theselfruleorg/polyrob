"""086 W0 §3.10: retiring a gone/dust book row — the measuring path, the agent
verb ``defi_data.retire_row`` and the owner verb ``/book retire``.

Neither entry takes a balance: the chain is read here, through the reconcile
seams, and a failed read, unknown decimals or an unpriced non-zero balance
refuse. Nothing is written without ``go`` on the owner seat.
"""
import types

import pytest

from core import open_positions as op
from core.open_positions import PositionDelta, apply_delta
from tools.defi.data_retire import RetireRowParams
from tools.defi.data_tool import DefiDataTool

ROBIN = "0xb200000000000000000000ea8625786a776539fb"
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _ctx(uid="owner-1"):
    return types.SimpleNamespace(user_id=uid, session_id="s-1")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    db = op.open_positions_db_path()
    apply_delta("owner-1", PositionDelta("base", ROBIN, "ROBIN", 0.16, 2.0), db_path=db)
    apply_delta("owner-1", PositionDelta("solana", MINT, "BONK", 5.0, 1.0), db_path=db)
    return tmp_path


def _tool(raw, *, price=None, conf="high", decimals=18):
    return DefiDataTool(
        holder="0xHOLDER",
        balances_fn=lambda h, c, toks: {t: raw for t in toks},
        identity_fn=lambda c, a: types.SimpleNamespace(symbol="ROBIN", decimals=decimals),
        price_fn=lambda c, a: (None if price is None
                               else types.SimpleNamespace(price_usd=price, confidence=conf)))


def _status(chain, addr):
    return op.get_position("owner-1", chain, addr, db_path=op.open_positions_db_path()).status


@pytest.mark.asyncio
async def test_agent_retires_a_zero_balance_row_with_proof(home):
    tool = _tool(0)
    res = await tool.retire_row(RetireRowParams(chain="base", address=ROBIN),
                                execution_context=_ctx())
    assert res.error is None, res.error
    assert _status("base", ROBIN) == "retired"
    from core.position_retire import retire_proof
    proof = retire_proof("owner-1", "base", ROBIN, db_path=op.open_positions_db_path())
    assert "balanceOf" in proof["proof"] and "0 raw" in proof["proof"]


@pytest.mark.asyncio
async def test_agent_retires_a_dust_balance_at_a_high_confidence_price(home):
    tool = _tool(10 ** 17, price=2.0)  # 0.1 token at $2 = $0.20
    res = await tool.retire_row(RetireRowParams(chain="base", address=ROBIN),
                                execution_context=_ctx())
    assert res.error is None, res.error


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_kw", [
    dict(raw=None),                                  # the read FAILED: unknown, not zero
    dict(raw=10 ** 18, price=5.0),                   # $5 held: not dust
    dict(raw=10 ** 17, price=2.0, conf="low"),       # no high-confidence price
    dict(raw=10 ** 17, price=None),                  # unpriced
    dict(raw=10 ** 17, price=2.0, decimals=None),    # decimals unknown
])
async def test_agent_refuses_anything_but_a_measured_zero_or_dust(home, tool_kw):
    raw = tool_kw.pop("raw")
    res = await _tool(raw, **tool_kw).retire_row(
        RetireRowParams(chain="base", address=ROBIN), execution_context=_ctx())
    assert res.error and "Not retired" in res.error
    assert _status("base", ROBIN) == "open"


@pytest.mark.asyncio
async def test_agent_refuses_an_untracked_row(home):
    res = await _tool(0).retire_row(
        RetireRowParams(chain="base", address="0x" + "2" * 40), execution_context=_ctx())
    assert res.error and "track no position" in res.error


@pytest.mark.asyncio
async def test_solana_absent_from_the_complete_enumeration_is_zero(home):
    tool = DefiDataTool(holder="0xHOLDER")
    tool._solana_holder = lambda: "Brs1111111111111111111111111111111111111111"
    tool._solana_tokens = lambda h: {}
    res = await tool.retire_row(RetireRowParams(chain="solana", address=MINT),
                                execution_context=_ctx())
    assert res.error is None, res.error
    assert _status("solana", MINT) == "retired"


@pytest.mark.asyncio
async def test_solana_failed_enumeration_refuses(home):
    tool = DefiDataTool(holder="0xHOLDER")
    tool._solana_holder = lambda: "Brs1111111111111111111111111111111111111111"
    tool._solana_tokens = lambda h: None
    res = await tool.retire_row(RetireRowParams(chain="solana", address=MINT),
                                execution_context=_ctx())
    assert res.error and "UNKNOWN" in res.error
    assert _status("solana", MINT) == "open"


@pytest.mark.asyncio
async def test_a_wrapped_native_row_is_not_retired(home):
    weth = "0x4200000000000000000000000000000000000006"
    apply_delta("owner-1", PositionDelta("base", weth, "WETH", 0.01, 30.0),
                db_path=op.open_positions_db_path())
    res = await _tool(0).retire_row(RetireRowParams(chain="base", address=weth),
                                    execution_context=_ctx())
    assert res.error and "wrapped native" in res.error


@pytest.mark.asyncio
async def test_owner_verb_previews_then_records(home):
    from tools.defi.retire import book_retire_reply
    tool = _tool(0)
    text = await book_retire_reply("owner-1", str(home), ["base", ROBIN], tool=tool)
    assert "Confirm" in text and f"/book retire base {ROBIN} go" in text
    assert _status("base", ROBIN) == "open"
    text = await book_retire_reply("owner-1", str(home), ["base", ROBIN, "go"], tool=tool)
    assert text.startswith("✅ Retired")
    assert _status("base", ROBIN) == "retired"


@pytest.mark.asyncio
async def test_owner_verb_usage(home):
    from tools.defi.retire import book_retire_reply
    assert "Usage: /book retire" in await book_retire_reply("owner-1", str(home), [])
    assert "Only the owner" in await book_retire_reply(None, str(home), ["base", ROBIN])


@pytest.mark.asyncio
async def test_telegram_book_retire_reaches_the_same_function(home, monkeypatch):
    from surfaces.telegram import admin_verbs
    seen = {}

    async def fake(user_id, data_dir, args, **kw):
        seen.update(user_id=user_id, args=list(args))
        return "ok"
    monkeypatch.setattr("tools.defi.retire.book_retire_reply", fake)
    call = admin_verbs.AdminCall(task_agent=None, result=None, cmd="/book", user_id="owner-1",
                                 data_dir=str(home), instance_id="i", args=["retire", "base", ROBIN],
                                 board=None)
    assert await admin_verbs.ADMIN_VERBS["/book"](call) == "ok"
    assert seen == {"user_id": "owner-1", "args": ["base", ROBIN]}


def test_book_retire_is_refused_in_a_room_but_the_read_is_not():
    from surfaces.telegram.harness import _room_refused_line
    assert _room_refused_line("/book", f"/book retire base {ROBIN} go")
    assert not _room_refused_line("/book", "/book")


def test_the_agent_verb_has_the_own_book_policy_row():
    from core.verb_policy_rows import CORE_VERB_ROWS
    row = CORE_VERB_ROWS["defi_data"]["defi_data_retire_row"]
    assert row["correspondent_blocked"] and row["room_denied"]
