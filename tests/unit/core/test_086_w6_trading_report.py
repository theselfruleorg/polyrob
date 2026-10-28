"""086 W6 — the ONE trading renderer (`core.trading_report`).

Pins:
 - no mandate and no position: one line, ``present`` False;
 - mandate line: status, preset, stage, allocation used at cost, slots;
 - a latched breaker names its reason; a running cooldown names its end;
 - exit coverage: a live exit job = PROTECTED; none / paused = UNPROTECTED;
   an unreadable cron store = UNKNOWN, never UNPROTECTED;
 - value: all priced = value of cost; part priced = "at least $X (N of M priced)";
 - realized P&L is gross, names the close reasons, says gas is not recorded;
 - the mandate's LLM cost = episodes with meta.mandate; no store = UNKNOWN;
 - an unreadable mandate store / book is UNKNOWN, never "none";
 - ``record_run(mandate=…)`` stamps ``meta["mandate"]``.
"""
import asyncio
import json
import sqlite3

import pytest

from core import trading_mandate as tm
from core.open_positions import PositionDelta, apply_delta
from core.trading_report import render_trading, trading_view

MEME = "0xb200000000000000000000ea8625786a776539fb"
MEME2 = "0xb200000000000000000000b344cb4a1e8bd51968"


@pytest.fixture
def home(tmp_path):
    return {"mandate_path": str(tmp_path / "trading.db"),
            "book_path": str(tmp_path / "open_positions.db"),
            "cron_db": str(tmp_path / "cron.db"),
            "memory_db": str(tmp_path / "memory.db")}


def _mandate(home, now=1000.0):
    return tm.create("u1", actor=tm.OWNER_SEAT, budget_usd=100, chains=["base"],
                     path=home["mandate_path"], now=now)


def _buy(home, addr=MEME, sym="MEME", cost=10.0, now=2000.0):
    apply_delta("u1", PositionDelta(chain="base", address=addr, symbol=sym, qty=100.0,
                                    cost_usd=cost), db_path=home["book_path"], now=now)


def _cron(home, jobs):
    conn = sqlite3.connect(home["cron_db"])
    conn.execute("CREATE TABLE cron_jobs (id TEXT, user_id TEXT, status TEXT, "
                 "enabled INTEGER, payload TEXT)")
    for i, (status, enabled, payload) in enumerate(jobs):
        conn.execute("INSERT INTO cron_jobs VALUES (?,?,?,?,?)",
                     (f"j{i}", "u1", status, enabled, json.dumps(payload)))
    conn.commit()
    conn.close()


def _episodes(home, rows):
    conn = sqlite3.connect(home["memory_db"])
    conn.execute("CREATE TABLE episodes (user_id TEXT, spend_usd REAL, meta TEXT)")
    for uid, usd, meta in rows:
        conn.execute("INSERT INTO episodes VALUES (?,?,?)", (uid, usd, json.dumps(meta)))
    conn.commit()
    conn.close()


def _text(home, **kw):
    return "\n".join(trading_view("u1", now=3000.0, **home, **kw)["lines"])


def test_nothing_is_one_line(home):
    v = trading_view("u1", now=1.0, **home)
    assert v["present"] is False and v["lines"] == ["trading: no mandate and no open position"]


def test_mandate_positions_coverage_and_cost(home):
    m = _mandate(home)
    _buy(home)
    _buy(home, addr=MEME2, sym="MEME2", cost=5.0)
    pos = {"chain": "base", "account": "", "address": MEME}
    _cron(home, [("scheduled", 1, {"mandate": m.id, "position": pos}),
                 ("paused", 1, {"mandate": m.id, "position": dict(pos, address=MEME2)}),
                 ("scheduled", 1, {"digest": True})])
    _episodes(home, [("u1", 0.5, {"mandate": m.id}), ("u1", 0.25, {"mandate": m.id}),
                     ("u1", 9.0, {"mandate": "other"}), ("u2", 9.0, {"mandate": m.id})])
    t = _text(home)
    assert f"mandate {m.id}: ACTIVE" in t and "preset memecoin" in t and "stage probe" in t
    assert "allocation $15.00 of $100.00 at cost" in t and f"slots 2/{m.slots}" in t
    assert "breaker: none tripped" in t
    meme_line = next(ln for ln in t.splitlines() if "MEME on base" in ln)
    meme2_line = next(ln for ln in t.splitlines() if "MEME2 on base" in ln)
    assert "PROTECTED" in meme_line and "UNPROTECTED" not in meme_line
    assert "UNPROTECTED (exit job paused)" in meme2_line
    assert "LLM cost of this mandate: $0.75 over 2 recorded run(s)" in t


def test_partial_price_is_at_least(home):
    _mandate(home)
    _buy(home)
    _buy(home, addr=MEME2, sym="MEME2", cost=5.0)
    t = _text(home, price_fn=lambda c, a: 0.2 if a == MEME else None)
    assert "value at least $20.00 (1 of 2 priced)" in t
    assert "unrealized +$10.00" in t
    t = _text(home, price_fn=lambda c, a: 0.2)
    assert "value $40.00 of cost $15.00" in t


def test_unreadable_cron_store_is_unknown_not_unprotected(home):
    _mandate(home)
    _buy(home)
    open(home["cron_db"], "w").write("not a database")
    v = trading_view("u1", now=3000.0, **home)
    t = "\n".join(v["lines"])
    assert "exit UNKNOWN" in t and "UNPROTECTED" not in t and "exit jobs" in v["unknown"]


def test_latched_breaker_and_cooldown(home):
    m = _mandate(home)
    tm.latch(m.id, "breaker:drawdown", path=home["mandate_path"], now=1500.0)
    t = _text(home)
    assert "HALTED" in t and "LATCHED — breaker:drawdown" in t


def test_cooldown_line(home):
    m = _mandate(home)
    tm.set_cooldown(m.id, 1.0, path=home["mandate_path"], now=2900.0)
    assert "cooling down until" in _text(home)


def test_realized_gross_reasons_and_gas(home):
    _mandate(home)
    _buy(home)
    apply_delta("u1", PositionDelta(chain="base", address=MEME, symbol="MEME", qty=-100.0,
                                    proceeds_usd=4.0, close_reason="stop_loss"),
                db_path=home["book_path"], now=2500.0)
    t = _text(home)
    assert "realized P&L: -$6.00 gross" in t and "stop_loss 1" in t
    assert "gas not recorded" in t


def test_no_episode_store_is_unknown(home):
    _mandate(home)
    assert "LLM cost of this mandate: UNKNOWN" in _text(home)


def test_unreadable_mandate_store_is_unknown(home):
    open(home["mandate_path"], "w").write("garbage")
    v = trading_view("u1", now=1.0, **home)
    assert v["present"] and "mandate" in v["unknown"]
    assert "UNKNOWN" in v["lines"][0] and "not the same as none" in v["lines"][0]


def test_book_without_mandate(home):
    _buy(home)
    t = _text(home)
    assert "open positions: 1 in the book, no mandate" in t


def test_render_wraps(home):
    _buy(home)
    out = render_trading("u1", now=3000.0, **home)
    assert out.startswith("Trading:\n  ")


def test_record_run_stamps_the_mandate(monkeypatch):
    from types import SimpleNamespace
    import agents.task.agent.core.episodic_digest as ED
    seen = {}

    async def finalize(**kw):
        seen.update(kw)
    monkeypatch.setattr("modules.memory.episodic.finalize_episode", finalize)
    run = SimpleNamespace(done_text="ok", user_messages=[], artifacts=[], evidence=None,
                          spend_usd=0.4, steps=1, skills_loaded=[])
    asyncio.run(ED.record_run(regime="cron", kind="cron", session_id="s", user_id="u1",
                              rail_id="cron:j1", task="t", outcome="done", run=run,
                              summary="ok", meta={"job_id": "j1"}, mandate="m_1"))
    assert seen["meta"]["mandate"] == "m_1" and seen["spend_usd"] == 0.4


# -- the status snapshot's Trading block (core/status_money) ------------------------

def _money(tmp_path, monkeypatch):
    from core.status_money import _money_section
    ledger = {"runtime": {"available": True}, "treasury": {"available": True}}
    return _money_section("u1", ledger, data_dir=str(tmp_path))


def test_status_block_absent_without_mandate_or_position(tmp_path, monkeypatch):
    sec = _money(tmp_path, monkeypatch)
    assert not any(ln.startswith("trading") for ln in sec.lines)
    assert sec.data["trading"]["present"] is False


def test_status_block_present_with_a_mandate(tmp_path, monkeypatch):
    m = tm.create("u1", actor=tm.OWNER_SEAT, budget_usd=50, chains=["base"],
                  path=str(tmp_path / "trading.db"))
    sec = _money(tmp_path, monkeypatch)
    assert "trading:" in sec.lines
    assert any(f"mandate {m.id}: ACTIVE" in ln for ln in sec.lines)
    assert sec.data["trading"]["mandate"] == m.id


def test_status_block_unavailable_on_unreadable_store(tmp_path, monkeypatch):
    (tmp_path / "trading.db").write_text("garbage")
    sec = _money(tmp_path, monkeypatch)
    assert any(ln.startswith("trading: unavailable in part (mandate") for ln in sec.lines)
    assert sec.data["trading"]["state"] == "unavailable"
    assert any(h.key == "trading_unreadable" for h in sec.health)


# -- the digest's Trading section (cron/digest) -------------------------------------

def test_digest_trading_section(tmp_path):
    from cron.digest import _trading_lines
    assert _trading_lines("u1", str(tmp_path)) == []  # nothing: absent
    m = tm.create("u1", actor=tm.OWNER_SEAT, budget_usd=50, chains=["base"],
                  path=str(tmp_path / "trading.db"))
    out = _trading_lines("u1", str(tmp_path))
    assert out[0] == "• Trading:" and any(f"mandate {m.id}" in ln for ln in out)


def test_digest_trading_section_names_unreadable(tmp_path):
    from cron.digest import _trading_lines
    (tmp_path / "trading.db").write_text("garbage")
    out = _trading_lines("u1", str(tmp_path))
    assert out[0].startswith("• Trading (could not read: mandate")


@pytest.mark.asyncio
async def test_compose_digest_carries_the_section(monkeypatch, tmp_path):
    from cron import digest
    monkeypatch.setattr(digest, "_trading_lines", lambda uid, d: ["• Trading:", "   x"])
    monkeypatch.setattr(digest, "_top_cost", lambda *a: "Top AI cost: none")
    for seam in ("_ledger", "_event_aggregate", "_open_asks", "_episodes"):
        monkeypatch.setattr(digest, seam, lambda *a: {} if seam != "_open_asks" else [])
    monkeypatch.setattr(digest, "_health_lines", lambda *a: [])
    monkeypatch.setattr(digest, "_missed_lines", lambda *a: [])
    text = await digest.compose_digest("u1", data_dir=str(tmp_path))
    assert "• Trading:\n   x" in text
