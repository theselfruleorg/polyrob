"""122 W4 — the income rule's readers (the existing records only), its loop (debt first, one buy
through the autonomous-act runner, a failed buy keeps the debt, an owner act is queued as a
decision, never forced) and the owner verb ``/wallet income``."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from core.wallet import income_rule as R
from core.wallet.token_trust import owner_seat_ctx
from tools.income_rule import loop as L
from tools.income_rule import sources as S

TOKEN = "0x" + "ab" * 20
NOW = 2_000_000.0
TX = "0x" + "2" * 64


@pytest.fixture
def env(tmp_path, monkeypatch):
    from core.wallet.signer import LocalEoaSigner
    from core.wallet.token_pins import pin
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "alice")
    signer = LocalEoaSigner(os.urandom(32))
    monkeypatch.setattr(R, "_signer", lambda: signer)
    pins = str(tmp_path / "pins.db")
    pin("base", TOKEN, "PNL", db_path=pins)
    path = str(tmp_path / "wallet" / R.STORE_NAME)
    monkeypatch.setattr(R, "store_path", lambda data_home=None: path)
    rule = R.set_rule(owner_seat_ctx("alice"), kinds=["fees", "payments"], percent=25,
                      chain="base", token=TOKEN, min_usd=10, path=path, now=NOW, pins_db=pins)
    return SimpleNamespace(path=path, pins=pins, rule=rule)


# ---------------------------------------------------------------------------- the readers

def test_fee_claims_read_the_audit_and_wait_for_a_confirmed_receipt(tmp_path):
    audit = tmp_path / "audit.jsonl"
    rows = [
        {"ts": NOW + 1, "action": "launchpad_claim", "chain": "base", "result_ref": TX,
         "income_raw": 2 * 10 ** 18},
        {"ts": NOW + 2, "action": "launchpad_claim", "chain": "base", "result_ref": "0xpend",
         "income_raw": 10 ** 18, "receipt_state": "pending_receipt"},
        {"ts": NOW + 3, "action": "launchpad_claim", "chain": "base", "result_ref": "0xold"},
        {"ts": NOW + 4, "action": "swap", "chain": "base", "result_ref": "0xswap"},
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    evs = S.fee_claims(audit_path=str(audit), price=lambda c, t: 3000.0)
    assert [(e.key, e.usd) for e in evs] == [(f"fees:{TX}", 6000.0)]


def test_nft_sales_read_the_sold_facts_net_of_royalty():
    state = {"lost": [
        {"chain": "base", "collection": "0xC0", "token_id": 7, "label": "A #7", "lost_at": NOW + 1,
         "left": {"tx": TX, "sold": {"price": 10 ** 18, "royalty": 10 ** 17,
                                     "currency": "0x" + "0" * 40}}},
        {"chain": "base", "collection": "0xC0", "token_id": 8, "lost_at": NOW + 2,
         "left": {"tx": "0x9", "sold": None}},                  # moved, not sold
    ]}
    evs = S.nft_sales(state=state, price=lambda c, t: 2000.0)
    assert len(evs) == 1 and evs[0].usd == pytest.approx(1800.0) and evs[0].kind == "nft"


def test_payments_read_completed_invoices():
    class DB:
        async def fetch_all(self, sql, args):
            assert "status = 'completed'" in sql
            return [{"id": "inv1", "amount_usd": 12.0, "chain": "base",
                     "completed_at": "2026-11-20 10:00:00", "updated_at": None},
                    {"id": "inv2", "amount_usd": 3.0, "chain": "base",
                     "completed_at": None, "updated_at": None}]
    evs = asyncio.run(S.payments(DB(), since=NOW))
    assert [(e.key, e.usd) for e in evs] == [("payments:inv1", 12.0)]


def test_realized_totals_cover_the_treasury_and_every_account(monkeypatch):
    from core import open_positions as op
    rows = [op.RealizedEntry(chain="base", address="0xT", symbol="T", account="", realized_usd=5,
                             known_legs=1, unknown_legs=0, sold_qty=1, updated_ts=1),
            op.RealizedEntry(chain="base", address="0xT", symbol="T", account="0xacct",
                             realized_usd=7, known_legs=1, unknown_legs=0, sold_qty=1,
                             updated_ts=1)]
    seen = {}

    def fake(uid, *, db_path=None, all_accounts=False):
        seen["all"] = all_accounts
        return rows
    monkeypatch.setattr(op, "realized_for", fake)
    totals = S.realized_totals("alice")
    assert seen["all"] is True
    assert totals["base:treasury:0xt"]["usd"] == 5 and totals["base:0xacct:0xt"]["usd"] == 7


# ---------------------------------------------------------------------------- the loop

class Cards:
    def __init__(self):
        self.made = []

    def changed_since(self, user_id, since, limit=60):
        return [c for c in self.made]

    def get(self, cid):
        return next((c for c in self.made if c.card_id == cid), None)


def _reader(events):
    async def read(rule, *, user_id):
        return events, None, []
    return read


def _tick(env, **kw):
    sent = []

    async def deliver(container, uid, text, source=""):
        sent.append(text)
    base = dict(user_id="alice", path=env.path, now=NOW + 10, price=lambda chain: 2500.0,
                cards=Cards(), deliver=deliver)
    base.update(kw)
    return asyncio.run(L.tick(None, **base)), sent


def _ev(key, usd):
    return R.IncomeEvent(key=key, kind="fees", at=NOW + 1, usd=usd, detail=f"claim {key}")


def test_no_rule_file_means_no_work(tmp_path):
    out = asyncio.run(L.tick(None, path=str(tmp_path / "none.json"), user_id="alice"))
    assert out["buy"] is None and not os.path.exists(tmp_path / "none.json")


def test_a_due_buy_runs_once_through_the_autonomous_act_runner(env):
    calls = []

    async def run(**kw):
        calls.append(kw)
        return {"ok": True, "text": f"RESULT: CONFIRMED tx {TX}", "tx_ref": TX,
                "needs_owner": False}
    out, sent = _tick(env, read=_reader([_ev("a", 60.0)]), run=run)
    assert out["buy"] == R.B_DONE and len(calls) == 1
    c = calls[0]
    assert c["action"] == "defi_trade_swap" and c["live"] is True
    assert c["params"]["token_in"] == "native" and c["params"]["token_out"].lower() == TOKEN
    assert c["params"]["amount_in"] == pytest.approx(15.0 / 2500.0)
    assert c["source"] == f"income rule {env.rule.id}" and "income rule" in c["why"]
    state = R.load(env.path)
    assert R.owed_usd(state) == 0 and state["buys"][-1]["tx"] == TX
    assert any("buy done" in s for s in sent)
    # the same income is never counted again
    out, _ = _tick(env, read=_reader([_ev("a", 60.0)]), run=run, now=NOW + 20)
    assert out["added"] == 0 and len(calls) == 1


def test_a_failed_buy_keeps_the_debt_and_tells_the_owner(env):
    async def run(**kw):
        return {"ok": False, "text": "refused: the wallet is paused", "tx_ref": "",
                "needs_owner": False}
    out, sent = _tick(env, read=_reader([_ev("a", 60.0)]), run=run)
    assert out["buy"] == R.B_FAILED
    assert R.owed_usd(R.load(env.path)) == 15.0
    assert any("debt stays" in s and "paused" in s for s in sent)


def test_a_buy_of_unknown_outcome_is_held_never_bought_again(env):
    """Audit 2026-11-21 M-4: a broadcast whose end is unknown keeps its income held and is
    not bought again after RETRY_SEC; the owner settles it."""
    calls = []

    async def run(**kw):
        calls.append(kw)
        return {"ok": False, "outcome": "unknown", "tx_ref": TX, "needs_owner": False,
                "text": f"RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. tx: {TX}"}
    out, sent = _tick(env, read=_reader([_ev("a", 60.0)]), run=run)
    state = R.load(env.path)
    assert out["buy"] == R.B_UNKNOWN and state["buys"][-1]["tx"] == TX
    assert R.owed_usd(state) == 0
    assert any("end unknown" in s for s in sent)
    _tick(env, read=_reader([]), run=run, now=NOW + 10 + R.RETRY_SEC + R.DAY_SEC)
    assert len(calls) == 1


def test_a_reverted_buy_is_failed_and_keeps_the_debt(env):
    """Audit 2026-11-21 M-4: a reverted buy has no error; it is never booked done."""
    async def run(**kw):
        return {"ok": False, "outcome": "failed", "tx_ref": TX, "needs_owner": False,
                "text": f"RESULT: REVERTED ON-CHAIN — it did NOT happen. tx: {TX}"}
    out, _ = _tick(env, read=_reader([_ev("a", 60.0)]), run=run)
    state = R.load(env.path)
    assert out["buy"] == R.B_FAILED and R.owed_usd(state) == 15.0


def test_a_buy_that_needs_the_owner_is_queued_as_a_decision_never_forced(env, monkeypatch):
    made = []

    def proposal_card(user_id, verb, args, *, why="", st=None, **k):
        card = SimpleNamespace(card_id="c1", parent_id=None, state="open", outcome="",
                               result="", verb=verb, args=args, expired=lambda now=None: False)
        made.append(card)
        st.made.append(card)
        return card

    async def push(container, user_id, card, **k):
        return "sent"
    monkeypatch.setattr("core.surfaces.cards.proposal_card", proposal_card)
    monkeypatch.setattr("core.surfaces.cards.push", push)

    async def run(**kw):
        return {"ok": False, "text": "needs the owner's approval", "tx_ref": "",
                "needs_owner": True}
    st = Cards()
    out, sent = _tick(env, read=_reader([_ev("a", 60.0)]), run=run, cards=st)
    assert out["buy"] == R.B_QUEUED
    assert made[0].verb == "/swap" and "go" not in made[0].args
    assert made[0].args[1:3] == ["native", "to"] and made[0].args[3].lower() == TOKEN
    state = R.load(env.path)
    assert R.open_buy(state)["card"] == "c1" and R.owed_usd(state) == 0
    # the owner confirmed: the quote card (a child) sent it
    st.made.append(SimpleNamespace(card_id="q1", parent_id="c1", state="done", outcome="sent",
                                   result=f"CONFIRMED tx {TX}", expired=lambda now=None: False))
    out, sent = _tick(env, read=_reader([]), run=run, cards=st, now=NOW + 30)
    state = R.load(env.path)
    assert state["buys"][-1]["state"] == R.B_DONE and state["buys"][-1]["tx"] == TX


def test_an_owner_confirmed_buy_that_reverted_keeps_the_debt(env):
    """Audit 2026-11-21 M-4: the owner's card "sent" a buy that reverted — not done."""
    state = R.load(env.path)
    R.accrue(state, env.rule, [_ev("a", 60.0)], NOW + 1)
    buy = R.start_buy(state, env.rule, NOW + 2)
    R.queue_buy(state, buy["id"], "c9", "waits")
    st = Cards()
    st.made.append(SimpleNamespace(card_id="c9", parent_id=None, state="done", outcome="",
                                   result="", expired=lambda now=None: False))
    st.made.append(SimpleNamespace(card_id="q9", parent_id="c9", state="done", outcome="sent",
                                   result=f"RESULT: REVERTED ON-CHAIN — it did NOT happen. "
                                          f"tx: {TX}", expired=lambda now=None: False))
    ended = L.follow_queued(state, "alice", st, NOW + 30)
    assert ended["state"] == R.B_FAILED and R.owed_usd(state) == 15.0


def test_the_proposal_words_are_a_card_the_owner_seat_can_carry(tmp_path):
    from core.surfaces import cards
    rule = R.Rule(id="r", created_at=0, kinds=["fees"], percent=25, chain="base", token=TOKEN)
    words = L.proposal_words(L.buy_params(rule, 15.0, 2500.0))
    card = cards.proposal_card("alice", "/swap", words, why="income rule",
                               st=cards.CardStore(str(tmp_path / "cards.db")))
    assert card.line == "/swap 0.006 native to " + TOKEN + " on base max 16.51"


def test_a_decision_that_ends_with_no_buy_gives_the_debt_back(env, monkeypatch):
    def proposal_card(user_id, verb, args, *, why="", st=None, **k):
        card = SimpleNamespace(card_id="c2", parent_id=None, state="open", outcome="",
                               result="", expired=lambda now=None: False)
        st.made.append(card)
        return card

    async def push(*a, **k):
        return "sent"
    monkeypatch.setattr("core.surfaces.cards.proposal_card", proposal_card)
    monkeypatch.setattr("core.surfaces.cards.push", push)

    async def run(**kw):
        return {"ok": False, "text": "x", "tx_ref": "", "needs_owner": True}
    st = Cards()
    _tick(env, read=_reader([_ev("a", 60.0)]), run=run, cards=st)
    st.made[0].state = "cancelled"
    _tick(env, read=_reader([]), run=run, cards=st, now=NOW + 30)
    state = R.load(env.path)
    assert state["buys"][-1]["state"] == R.B_FAILED and R.owed_usd(state) == 15.0


def test_a_running_buy_cut_by_a_restart_is_unknown_and_keeps_its_income(env):
    state = R.load(env.path)
    R.accrue(state, env.rule, [_ev("a", 60.0)], NOW + 1)
    buy = R.start_buy(state, env.rule, NOW + 2)
    R.save(env.path, state)

    async def run(**kw):
        raise AssertionError("no new buy: nothing is owed")
    out, sent = _tick(env, read=_reader([]), run=run, now=NOW + 2 + L.RUNNING_STALE_SEC + 1)
    state = R.load(env.path)
    assert state["buys"][-1]["state"] == R.B_UNKNOWN and R.owed_usd(state) == 0
    assert any("end unknown" in s for s in sent) and buy["id"] in sent[0]


def test_an_unreadable_store_moves_nothing(env):
    with open(env.path, "w") as fh:
        fh.write("{broken")
    out, sent = _tick(env, read=_reader([_ev("a", 60.0)]))
    assert out["errors"] and out["buy"] is None and sent == []


# ---------------------------------------------------------------------------- the owner verb

def test_the_owner_verb_quotes_then_sets_with_go(env, monkeypatch):
    from tools.income_rule.owner_verb import income_reply
    monkeypatch.setattr(R, "check_target", lambda chain, token, pins_db=None:
                        {"chain": "base", "address": TOKEN, "symbol": "PNL"})
    quote = income_reply(["fees,nft", "30%", "to", TOKEN, "on", "base", "min", "20"], "alice")
    assert "Add `go`" in quote and "30 %" in quote
    assert R.rule_of(R.load(env.path)).percent == 25            # a quote changes nothing
    done = income_reply(["fees,nft", "30%", "to", TOKEN, "on", "base", "min", "20", "go"],
                        "alice")
    assert done.startswith("✅")
    rule = R.rule_of(R.load(env.path))
    assert rule.percent == 30 and rule.kinds == ["fees", "nft"] and rule.min_usd == 20
    assert "Owed to the rule" in income_reply([], "alice")
    assert income_reply(["end", "go"], "alice").startswith("✅")
    assert R.rule_of(R.load(env.path)) is None
    assert income_reply(["bogus"], "alice").startswith("❌")


def test_the_spend_record_carries_what_a_claim_received():
    from core.money.ledger import SpendLedger
    g = SpendLedger(max_per_tx_usd=100)
    g.record(venue="defi", action="swap", amount_usd=1, counterparty=None,
             idempotency_key=None, result_ref=TX)
    g.record(venue="defi", action="launchpad_claim", amount_usd=0.02, counterparty=None,
             idempotency_key=None, result_ref=TX, income_raw=5 * 10 ** 17)
    assert "income_raw" not in g.audit_log[0]               # byte-identical when absent
    assert g.audit_log[1]["income_raw"] == 5 * 10 ** 17
