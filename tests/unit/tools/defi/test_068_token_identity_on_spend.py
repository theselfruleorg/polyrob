"""068 W0: which contract a buy may name, enforced by the verb.

2026-09-25: a PNL-buyback cron bought 33.7M of an airdropped look-alike
(`0x357A…`, "Pissin N Lying") for $134.54. `token_info` said `verified: false`,
the route check said `UNAVAILABLE`, and the swap read neither.
"""
from types import SimpleNamespace

import pytest

from core.wallet import token_pins
from tools.defi import identity_gate
from tools.defi.identity_gate import buy_identity_refusal

REAL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"
CTX = SimpleNamespace(user_id="rob", metadata={})


@pytest.fixture
def pins_db(tmp_path, monkeypatch):
    path = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: path)
    return path


@pytest.fixture
def no_positions(monkeypatch):
    import core.open_positions as op
    monkeypatch.setattr(op, "entries_for", lambda uid, **kw: {})


def _ident(address, symbol="PNL", name="Pissin N Lying", verified=False):
    return SimpleNamespace(address=address, symbol=symbol, name=name,
                           verified=verified, decimals=18)


# ---- the store -------------------------------------------------------------

def test_pin_replaces_the_old_address_for_one_symbol(pins_db):
    token_pins.pin("robinhood", FAKE, "pnl")
    row = token_pins.pin("robinhood", REAL, "PNL", note="track record")
    assert row["replaced"].lower() == FAKE.lower()
    assert token_pins.pinned_addresses_for_symbol("robinhood", "pnl") == [REAL]
    assert token_pins.owner_pin("robinhood", FAKE) is None


def test_missing_store_reads_as_no_pins(pins_db):
    assert token_pins.all_pins() == []
    assert token_pins.unpin("robinhood", REAL) is False


def test_owner_pin_makes_identity_verified(monkeypatch, pins_db):
    import core.wallet.tokens as tokens
    token_pins.pin("robinhood", REAL, "PNL")
    monkeypatch.setattr(tokens, "_identity_unpinned", lambda c, a, **kw: tokens.TokenIdentity(
        chain=c, address=a, symbol="PNL", name="Rob Track Record", decimals=18,
        verified=False, metadata_changed=False, source="frozen"))
    real = tokens.get_token_identity("robinhood", REAL)
    fake = tokens.get_token_identity("robinhood", FAKE)
    assert real.verified and real.source == "owner_pin"
    assert not fake.verified


# ---- the gate --------------------------------------------------------------

def test_the_incident_is_refused_once_the_owner_pins(pins_db, no_positions):
    token_pins.pin("robinhood", REAL, "PNL")
    why = buy_identity_refusal(chain="robinhood", token_out=FAKE, id_out=_ident(FAKE),
                               max_spend_usd=140.0, route_verdict="UNAVAILABLE",
                               execution_context=CTX)
    assert why and "PINNED" in why and REAL in why


def test_the_incident_is_refused_with_no_pin_at_all(pins_db, no_positions):
    why = buy_identity_refusal(chain="robinhood", token_out=FAKE, id_out=_ident(FAKE),
                               max_spend_usd=140.0, route_verdict="UNAVAILABLE",
                               execution_context=CTX)
    assert why and "UNVERIFIED" in why and "$5.00" in why


def test_second_contract_for_a_held_symbol_is_refused(pins_db, monkeypatch):
    import core.open_positions as op
    held = SimpleNamespace(chain="robinhood", address=REAL, symbol="PNL")
    monkeypatch.setattr(op, "entries_for", lambda uid, **kw: {REAL.lower(): held})
    # Even a $1 ticket that the route check AGREES on: the name is the problem.
    why = buy_identity_refusal(chain="robinhood", token_out=FAKE, id_out=_ident(FAKE),
                               max_spend_usd=1.0, route_verdict="AGREES",
                               execution_context=CTX)
    assert why and "two contracts" in why


def test_the_pinned_contract_itself_passes(pins_db, no_positions):
    token_pins.pin("robinhood", REAL, "PNL")
    assert buy_identity_refusal(
        chain="robinhood", token_out=REAL, id_out=_ident(REAL, verified=True),
        max_spend_usd=140.0, route_verdict="UNAVAILABLE", execution_context=CTX) is None


@pytest.mark.parametrize("usd,verdict", [(5.0, "UNAVAILABLE"), (2.5, "UNAVAILABLE"),
                                         (140.0, "AGREES")])
def test_scouting_tickets_and_checked_routes_are_untouched(pins_db, no_positions, usd, verdict):
    assert buy_identity_refusal(
        chain="base", token_out=FAKE, id_out=_ident(FAKE, symbol="MEME"),
        max_spend_usd=usd, route_verdict=verdict, execution_context=CTX) is None


def test_owner_direct_cli_call_is_not_gated(pins_db, no_positions):
    token_pins.pin("robinhood", REAL, "PNL")
    assert buy_identity_refusal(chain="robinhood", token_out=FAKE, id_out=_ident(FAKE),
                                max_spend_usd=140.0, route_verdict="UNAVAILABLE",
                                execution_context=None) is None


def test_the_ceiling_is_the_top_degen_ticket():
    assert identity_gate.UNVERIFIED_UNCHECKED_MAX_USD == 5.0


# ---- the swap verb calls it ------------------------------------------------

def test_swap_consults_the_gate_before_broadcast():
    import inspect
    from tools.defi.trade_tool import DefiTradeTool
    src = inspect.getsource(DefiTradeTool.swap)
    assert src.index("buy_identity_refusal") < src.index("_run_guarded")
    assert "asset=" in src  # G5: the audit row names the pair


# ---- G4: sell what you hold -------------------------------------------------

@pytest.mark.asyncio
async def test_approve_clamps_a_rounding_overshoot_to_the_held_balance():
    from tests.unit.tools.defi.test_trade_t4 import ROUTER, USDC, _Rail, _tool
    from tools.defi.trade_tool import ApproveParams
    captured = []
    tool, _ = _tool(price=1.0, captured=captured, balance=4_999_999)
    try:
        await tool.approve_token(ApproveParams(
            token=USDC, spender=ROUTER, amount=5.0, max_spend_usd=6.0, dry_run=True))
    finally:
        _Rail.last = None
    (grant,) = captured[0].expected_allowance_grants
    assert grant[2] == 4_999_999 == captured[0].held_balance_raw


@pytest.mark.asyncio
async def test_approve_keeps_a_real_overshoot():
    from tests.unit.tools.defi.test_trade_t4 import ROUTER, USDC, _Rail, _tool
    from tools.defi.trade_tool import ApproveParams
    captured = []
    tool, _ = _tool(price=1.0, captured=captured, balance=4_000_000)
    try:
        await tool.approve_token(ApproveParams(
            token=USDC, spender=ROUTER, amount=5.0, max_spend_usd=6.0, dry_run=True))
    finally:
        _Rail.last = None
    (grant,) = captured[0].expected_allowance_grants
    assert grant[2] == 5_000_000


# ---- reconcile names the collision first ------------------------------------

def test_reconcile_shows_the_look_alike_warning_before_any_row():
    from tools.defi import reconcile as rec
    ledger, _err = rec.parse_open_positions(
        "## Open positions\n\n| Token | Address | Qty |\n|---|---|---|\n"
        f"| PNL | {REAL} | 126902897.45 |\n")
    holdings = [
        rec.ChainHolding(address=FAKE, symbol="PNL", qty=615.17, raw_units=615,
                         value_usd=None, balance_known=True, name="Pissin N Lying"),
        rec.ChainHolding(address=REAL, symbol="PNL", qty=126902897.45, raw_units=1,
                         value_usd=None, balance_known=True, name="Rob Track Record"),
    ]
    report = rec.diff(ledger, holdings)
    out = rec.render(report, chain="robinhood", holder="0xabc", ledger_path="l.md",
                     coverage="full")
    warn = out.index("ONE SYMBOL, MORE THAN ONE CONTRACT")
    # 071 W3: the unpriced look-alike in neither book is `unsolicited`, not an issue
    assert warn < out.index("matched:") < out.index("unsolicited (")
    assert "Pissin N Lying" in out and "Rob Track Record" in out
    assert report.to_dict()["collisions"]


def test_portfolio_keeps_its_import_of_the_helper():
    from tools.defi.data_tool import _ticker_collision_lines
    from tools.defi.reconcile import _ticker_collision_lines as moved
    assert _ticker_collision_lines is moved


# ---- G3: a routine task does not pin the degen playbook ---------------------

@pytest.mark.parametrize("task", ["rotate the API token and update the ledger",
                                  "scan the repo and screen the diff",
                                  "claim the domain and check its position"])
def test_bare_common_words_do_not_load_the_trading_playbook(task):
    from agents.task.agent.skill_manager import SkillManager
    ids = {m.skill_id for m in SkillManager().get_skills_for_session(
        task=task, tool_ids=["defi_trade", "defi_data", "task"])}
    assert "treasury-trading" not in ids


def test_trading_tasks_still_load_it():
    from agents.task.agent.skill_manager import SkillManager
    ids = {m.skill_id for m in SkillManager().get_skills_for_session(
        task="buy token PNL for the buyback", tool_ids=["defi_trade", "defi_data"])}
    assert "treasury-trading" in ids
