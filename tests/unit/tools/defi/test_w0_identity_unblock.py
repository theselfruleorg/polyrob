"""W0 (token-management evaluation): the prod PNL deadlock, reproduced and closed.

Prod: the wallet holds a tracked "PNL" at the look-alike 0x357A…, so rule 2 of
the identity gate refused every 6-hourly buyback of the REAL PNL 0xbBa6… (the
instance's own Pons launch) and told the owner to run a server CLI command.

After W0:
* our own launch is trusted, the look-alike row is QUARANTINED, the buy passes;
* an OWNER-authored ``target_token`` is trust for its run; an agent-authored one
  stays restrict-only;
* when neither side is trusted, rule 2 still refuses;
* no refusal names a shell command.
"""
import os
import types

import pytest

from core import open_positions as op
from core.wallet import token_pins
from core.wallet import token_provenance as tp
from tools.defi.identity_gate import buy_identity_refusal

REAL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"


def _ctx(target=None, uid="rob"):
    meta = {"money_target": target} if target else {}
    return types.SimpleNamespace(user_id=uid, metadata=meta, role="orchestrator",
                                 is_sub_agent=False)


def _ident(symbol="PNL", verified=False, source="frozen"):
    return types.SimpleNamespace(symbol=symbol, name="Rob Track Record",
                                 verified=verified, source=source)


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    tp._reset_for_tests()
    monkeypatch.setattr(tp, "_PROBES", {})
    pins = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: pins)
    book = str(tmp_path / "open_positions.db")
    monkeypatch.setattr(op, "open_positions_db_path", lambda data_dir=None: book)
    # the look-alike bought by mistake on 2026-09-25, $134.54
    op.apply_delta("rob", op.PositionDelta(chain="robinhood", address=FAKE.lower(),
                                           symbol="PNL", qty=33_700_000.0,
                                           cost_usd=134.54))
    yield book
    tp._reset_for_tests()


def _buy(ctx=None, ident=None, usd=50.0, verdict="UNAVAILABLE"):
    return buy_identity_refusal(chain="robinhood", token_out=REAL,
                                id_out=ident or _ident(), max_spend_usd=usd,
                                route_verdict=verdict, execution_context=ctx or _ctx())


def _status(address=FAKE):
    return op.get_position("rob", "robinhood", address).status


def test_today_neither_side_is_trusted_and_the_buy_is_refused_without_a_shell_command():
    why = _buy()
    assert why and "tracked PNL position" in why and "not trusted" in why
    assert "polyrob" not in why and "`" not in why and "pin-token" not in why
    assert "ask" in why.lower() and FAKE.lower() in why.lower() and REAL in why
    assert _status() == "open"


def test_our_own_launch_passes_and_quarantines_the_lookalike():
    tp.record_own_token("robinhood", REAL, kind="launchpad_launch", evidence="tx")
    assert _buy() is None
    e = op.get_position("rob", "robinhood", FAKE)
    assert e.status == "quarantined" and "look-alike" in e.status_reason
    assert e.entry_usd == pytest.approx(134.54)          # honest P&L kept
    assert _buy() is None                                 # and the next cycle too


def test_the_onchain_probe_finds_the_prod_launch_with_zero_owner_action(monkeypatch):
    monkeypatch.setitem(tp._PROBES, "pons",
                        lambda chain, addr: "deployer 0xcAda" if addr.lower() == REAL.lower() else None)
    assert _buy(usd=134.0) is None
    assert _status() == "quarantined"
    assert tp.own_token("robinhood", REAL) is not None    # recorded for every later read


def test_an_owner_authored_target_is_trust_for_its_run():
    target = {"chain": "robinhood", "address": REAL, "authored_by": "owner"}
    assert _buy(ctx=_ctx(target)) is None
    assert _status() == "quarantined"


def test_an_agent_authored_target_stays_restrict_only():
    target = {"chain": "robinhood", "address": REAL}
    why = _buy(ctx=_ctx(target))
    assert why and "tracked PNL position" in why
    assert _status() == "open"


def test_an_owner_target_does_not_trust_a_different_contract():
    target = {"chain": "robinhood", "address": FAKE, "authored_by": "owner"}
    assert _buy(ctx=_ctx(target))


def test_an_owner_pin_quarantines_the_lookalike_too():
    token_pins.pin("robinhood", REAL, "PNL")
    assert _buy(ident=_ident(verified=True, source="owner_pin")) is None
    assert _status() == "quarantined"


def test_a_trusted_claimant_is_never_quarantined():
    # both contracts are ours (two launches with one symbol): no look-alike
    tp.record_own_token("robinhood", REAL, kind="launchpad_launch")
    tp.record_own_token("robinhood", FAKE, kind="launchpad_launch")
    assert _buy() is None
    assert _status() == "open"


def test_a_quarantined_row_is_no_longer_a_claim():
    op.set_status("rob", "robinhood", FAKE, "quarantined", reason="x")
    # the real token is untrusted here; only rule 3 (the $5 ticket) applies now
    assert _buy(usd=5.0) is None
    why = _buy(usd=50.0)
    assert why and "UNVERIFIED" in why and "tracked" not in why
    assert "polyrob" not in why and "`" not in why


def test_buying_the_lookalike_itself_is_still_refused_under_the_owner_target():
    tp.record_own_token("robinhood", REAL, kind="launchpad_launch")
    op.apply_delta("rob", op.PositionDelta(chain="robinhood", address=REAL.lower(),
                                           symbol="PNL", qty=1.0, cost_usd=1.0))
    why = buy_identity_refusal(chain="robinhood", token_out=FAKE, id_out=_ident(),
                               max_spend_usd=50.0, route_verdict="UNAVAILABLE",
                               execution_context=_ctx())
    assert why


def test_an_unreadable_book_still_refuses_an_untrusted_buy(_stores):
    with open(_stores, "wb") as fh:
        fh.write(b"not a database" * 200)
    why = _buy()
    assert why and "could not be read" in why and "polyrob" not in why


def test_an_unreadable_book_does_not_block_our_own_launch(_stores):
    tp.record_own_token("robinhood", REAL, kind="launchpad_launch")
    with open(_stores, "wb") as fh:
        fh.write(b"not a database" * 200)
    assert _buy() is None


# ---- the run carries the author ---------------------------------------------

class _TaskAgent:
    def __init__(self):
        self.request = None

    async def create_session(self, user_id, request):
        self.request = request
        return {"id": "s1"}

    async def run_session(self, user_id, session_id):
        return "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("author,stamped", [("owner", True), ("agent", False), (None, False)])
async def test_the_cron_run_stamps_an_owner_target(author, stamped):
    from cron.jobs import CronJob
    from cron.runner import make_agent_runner
    payload = {"provider": "anthropic",
               "target_token": {"chain": "robinhood", "address": REAL.lower()}}
    if author:
        payload["authored_by"] = author
    ta = _TaskAgent()
    job = CronJob(id="j", task="PNL buyback", schedule_spec="6h", user_id="u1",
                  next_run_at=None, payload=payload)
    assert await make_agent_runner(ta)(job) is True
    assert (ta.request["money_target"].get("authored_by") == "owner") is stamped
    assert ta.request["money_target"]["address"] == REAL


def test_a_child_job_never_inherits_the_owner_stamp():
    from core.wallet.buy_target import inherit_target
    run = _ctx({"chain": "robinhood", "address": REAL, "authored_by": "owner"})
    assert inherit_target(run, None) == {"chain": "robinhood", "address": REAL}


def test_the_skills_never_send_the_owner_to_the_server_cli():
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                        "data", "prompts", "skills")
    for name in ("token-identity", "treasury-trading", "dca", "stable-cash",
                 "robinhood-chain"):
        path = os.path.join(root, name, "SKILL.md")
        if not os.path.isfile(path):
            continue
        text = open(path, encoding="utf-8").read()
        assert "polyrob wallet pin-token" not in text, name
