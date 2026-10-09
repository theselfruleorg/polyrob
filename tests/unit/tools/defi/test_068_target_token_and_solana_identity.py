"""068 G2 + the Solana half of G1.

G2: a goal/cron payload may declare `target_token` {chain, address}; it rides the
run as data and a buy of any other contract on that chain is refused. The
2026-09-25 buyback carried its target only in prose, and lost it.

G1 (Solana): `solana_swap` runs the same identity gate as the EVM swap, with the
mint's self-reported symbol from the GoPlus screen and verified = owner pin.
"""
import types

import pytest

from core.wallet.buy_target import normalize_target, target_refusal

REAL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
OTHER_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _ctx(target=None):
    return types.SimpleNamespace(user_id="rob", role="orchestrator", is_sub_agent=False,
                                 metadata={"turn_kind": None, "money_target": target})


# ---- normalize --------------------------------------------------------------

def test_normalize_checksums_evm_and_keeps_base58_verbatim():
    assert normalize_target({"chain": "Robinhood", "address": REAL.lower()}) == {
        "chain": "robinhood", "address": REAL}
    assert normalize_target({"chain": "solana", "address": f" {MINT} "}) == {
        "chain": "solana", "address": MINT}
    assert normalize_target(None) is None and normalize_target({}) is None


@pytest.mark.parametrize("bad", ["PNL", {"chain": "robinhood"}, {"address": REAL},
                                 {"chain": "nochain", "address": REAL},
                                 {"chain": "robinhood", "address": "0x123"}])
def test_a_malformed_target_is_refused_not_dropped(bad):
    with pytest.raises(ValueError):
        normalize_target(bad)


# ---- the refusal ------------------------------------------------------------

def test_target_refusal():
    t = {"chain": "robinhood", "address": REAL}
    assert target_refusal(_ctx(t), chain="robinhood", token_out=REAL) is None
    assert target_refusal(_ctx(t), chain="robinhood", token_out=REAL.lower()) is None
    why = target_refusal(_ctx(t), chain="robinhood", token_out=FAKE)
    assert why and REAL in why and FAKE in why
    # 068 B4: a target binds the whole run — another chain is refused too.
    assert target_refusal(_ctx(t), chain="base", token_out=FAKE)
    assert target_refusal(_ctx(None), chain="robinhood", token_out=FAKE) is None
    assert target_refusal(None, chain="robinhood", token_out=FAKE) is None


def test_base58_target_is_case_sensitive():
    t = {"chain": "solana", "address": MINT}
    assert target_refusal(_ctx(t), chain="solana", token_out=MINT) is None
    assert target_refusal(_ctx(t), chain="solana", token_out=MINT.lower())


# ---- plumbing ---------------------------------------------------------------

def test_session_request_carries_the_target():
    from agents.task.task_agent_support import SessionRequest
    t = {"chain": "robinhood", "address": REAL}
    req = SessionRequest(task="t", provider="p", model="m", money_target=t)
    assert req.__dict__["money_target"] == t  # persisted with the request


def test_step_context_and_both_swap_verbs_read_it():
    import inspect
    from agents.task.agent.core import step_execution
    from tools.defi.trade_tool import DefiTradeTool
    assert '"money_target"' in inspect.getsource(step_execution)
    for verb, send in ((DefiTradeTool.swap, "_run_guarded"),
                       (DefiTradeTool.solana_swap, "_solana_quote")):
        src = inspect.getsource(verb)
        assert "acquisition_refusal" in src and src.index("acquisition_refusal") < src.index(send)


class _TaskAgent:
    def __init__(self):
        self.request = None

    async def create_session(self, user_id, request):
        self.request = request
        return {"id": "s1"}

    async def run_session(self, user_id, session_id):
        return "done"


@pytest.mark.asyncio
async def test_the_cron_request_carries_a_normalized_target():
    from cron.jobs import CronJob
    from cron.runner import make_agent_runner
    ta = _TaskAgent()
    job = CronJob(id="j", task="PNL buyback", schedule_spec="1d", user_id="u1", next_run_at=None,
                  payload={"provider": "anthropic", "authored_by": "owner",
                           "target_token": {"chain": "robinhood", "address": REAL.lower()}})
    assert await make_agent_runner(ta)(job) is True
    # W0: an owner-seat job (stamped owner) — its target carries the owner stamp.
    assert ta.request["money_target"] == {"chain": "robinhood", "address": REAL,
                                          "authored_by": "owner"}


@pytest.mark.asyncio
async def test_a_cron_with_a_malformed_target_does_not_run():
    from cron.jobs import CronJob
    from cron.runner import make_agent_runner
    ta = _TaskAgent()
    job = CronJob(id="j", task="PNL buyback", schedule_spec="1d", user_id="u1", next_run_at=None,
                  payload={"provider": "anthropic", "target_token": "PNL"})
    assert await make_agent_runner(ta)(job) is False
    assert ta.request is None


@pytest.mark.asyncio
async def test_cronjob_schedule_validates_and_stores_the_target(tmp_path):
    from cron.jobs import CronJobStore
    from cron.service import CronService
    from tools.cronjob_tools import CronJobTool, CronScheduleAction
    t = object.__new__(CronJobTool)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    ctx = types.SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=False,
                                metadata={}, session_id=None)
    bad = await t.cronjob_schedule(CronScheduleAction(
        task="buy the PNL tranche", schedule="1d", target_token={"chain": "robinhood"}), execution_context=ctx)
    assert bad.error and "target_token" in bad.error
    ok = await t.cronjob_schedule(CronScheduleAction(
        task="buy the PNL tranche", schedule="1d", target_token={"chain": "robinhood", "address": REAL.lower()}),
        execution_context=ctx)
    assert ok.error is None
    assert t._cron_service.list_jobs(user_id="u1")[0].payload["target_token"] == {
        "chain": "robinhood", "address": REAL}


def test_owner_create_normalizes_and_refuses():
    from core.owner_create import create_goal
    seen = {}

    class _Board:
        def create(self, **kw):
            seen.update(kw)
            return kw
    create_goal(_Board(), user_id="rob", title="buyback",
                extra_payload={"target_token": {"chain": "robinhood", "address": REAL.lower()}})
    assert seen["payload"]["target_token"]["address"] == REAL
    with pytest.raises(ValueError):
        create_goal(_Board(), user_id="rob", title="buyback",
                    extra_payload={"target_token": {"chain": "robinhood", "address": "0x1"}})


# ---- GoPlus Solana metadata -------------------------------------------------

def test_solana_screen_carries_the_self_reported_symbol():
    from tools.defi.providers.goplus import parse_solana_screen
    v = parse_solana_screen({"code": 1, "result": {MINT: {
        "freezable": {"status": "0"}, "mintable": {"status": "0"},
        "metadata": {"symbol": "BONK", "name": "Bonk"}}}})
    assert v.available and v.symbol == "BONK" and v.name == "Bonk"


# ---- solana_swap runs the identity gate and the target ----------------------

def _solana_tool(monkeypatch, symbol="PNL"):
    pytest.importorskip("solders", reason="needs the `solana` extra")
    from tools.defi.providers.base import ScreenVerdict
    from tools.defi.trade_tool import DefiTradeTool
    from tests.unit.tools.defi.test_solana_swap import _Wallet
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    tool = DefiTradeTool(
        wallet=_Wallet(), solana_decimals_fn=lambda m: 6,
        solana_screen_fn=lambda m: ScreenVerdict(available=True, checks={"freezable": "no"},
                                                 symbol=symbol, name="Look Alike"),
        solana_quote_fn=lambda *a, **k: None)   # reaching the quote = the gates passed
    monkeypatch.setattr(tool, "_solana_turn_gate", lambda ctx, **kw: (None, False, False))
    import core.open_positions as op
    monkeypatch.setattr(op, "entries_for", lambda uid, **kw: {})
    return tool


def _sparams(**kw):
    from tools.defi.trade_tool import SolanaSwapParams
    base = dict(token_in=USDC_SOL, token_out=MINT, amount_in=10.0, max_spend_usd=10.0)
    base.update(kw)
    return SolanaSwapParams(**base)


@pytest.fixture
def pins(tmp_path, monkeypatch):
    from core.wallet import token_pins
    path = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: path)
    return token_pins


@pytest.mark.asyncio
async def test_unpinned_solana_buy_above_the_scouting_ticket_is_refused(monkeypatch, pins):
    tool = _solana_tool(monkeypatch)
    res = await tool.solana_swap(_sparams(), _ctx())
    assert res.error and "UNVERIFIED" in res.error


@pytest.mark.asyncio
async def test_scouting_ticket_and_pinned_mint_pass_the_gate(monkeypatch, pins):
    tool = _solana_tool(monkeypatch)
    res = await tool.solana_swap(_sparams(amount_in=5.0, max_spend_usd=5.0), _ctx())
    assert res.error and "no route" in res.error.lower()
    pins.pin("solana", MINT, "PNL")
    res = await tool.solana_swap(_sparams(), _ctx())
    assert res.error and "no route" in res.error.lower()


@pytest.mark.asyncio
async def test_solana_look_alike_of_a_pinned_symbol_is_refused(monkeypatch, pins):
    pins.pin("solana", OTHER_MINT, "PNL")
    tool = _solana_tool(monkeypatch)
    res = await tool.solana_swap(_sparams(amount_in=1.0, max_spend_usd=1.0), _ctx())
    assert res.error and "PINNED" in res.error


@pytest.mark.asyncio
async def test_solana_buy_outside_the_run_target_is_refused(monkeypatch, pins):
    pins.pin("solana", MINT, "MEME")
    tool = _solana_tool(monkeypatch, symbol="MEME")
    res = await tool.solana_swap(_sparams(), _ctx({"chain": "solana", "address": OTHER_MINT}))
    assert res.error and "declares its target" in res.error
