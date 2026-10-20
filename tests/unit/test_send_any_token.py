"""A wallet sends any token it holds (owner, 2026-10-17) — the surfaces.

Prod: six USDG bounty cards refused at "Get a quote" (one pool, so "low"), the
card died with no next step, and the agent never saw the refusal and told the
owner the card had "expired". Covered here: the stablecoin pin and peg, the
ETH anchor for an L2's WETH, the `/send … anyway` extra step on the seat and
the card, and the owner-thread delta.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.surfaces import cards
from core.wallet import chains, tokens
from core.wallet.tx_guard_steps.value import UNVERIFIED_MARK
from surfaces.telegram import send_ops
from tools.intel import cache as intel_cache
from tools.intel import price as P
from tools.intel.model import HIGH, LOW, SourceAnswer

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
TO = "0x6612bBa2fCA627c5E876bE8a1Ffd62ce88e801D4"
TOKEN = "0x" + "ab" * 20


# -- the pin -------------------------------------------------------------------

def test_usdg_is_a_pinned_stable_with_its_own_identity():
    assert USDG.lower() in chains.usd_pegged("robinhood")
    pin = tokens.canonical_token("robinhood", USDG)
    assert pin == {"symbol": "USDG", "name": "Global Dollar", "decimals": 6}


def test_base_usdc_is_pegged_and_a_read_only_row_is_not():
    assert chains.get("base").usdc.lower() in chains.usd_pegged("base")
    assert chains.usd_pegged("no-such-chain") == frozenset()


# -- the anchors ---------------------------------------------------------------

@pytest.fixture
def registry():
    saved = P.registered_sources()
    P.unregister_all()
    intel_cache.clear_all()
    yield
    P.unregister_all()
    for s in saved:
        P.register_source(s.name, s.fn, covers=s.covers, primary=s.primary)
    intel_cache.clear_all()


def _ds(usd, conf):
    return SourceAnswer(name="dexscreener", usd=usd, liquidity_usd=1_260_000.0,
                        priced_liquidity_usd=1_260_000.0, pool="0xpool", pool_count=1,
                        confidence=conf)


def test_a_pinned_stable_on_its_peg_is_spend_grade(registry):
    P.register_source("dexscreener", lambda c, a: _ds(0.9998, LOW), primary=True)
    P.register_source("geckoterminal", lambda c, a: SourceAnswer("geckoterminal", usd=1.0012))
    q = P.quote("robinhood", USDG, use_cache=False)
    assert q.confidence == HIGH and "peg" in q.reason
    assert P.spend_price("robinhood", USDG) == pytest.approx(1.0005, abs=0.001)


def test_a_pinned_stable_off_its_peg_keeps_the_pool_grade(registry):
    P.register_source("dexscreener", lambda c, a: _ds(0.93, LOW), primary=True)
    q = P.quote("robinhood", USDG, use_cache=False)
    assert q.confidence == LOW and "OFF its" in (q.note or "")
    assert P.spend_price("robinhood", USDG) is None


def test_an_unpinned_one_pool_token_is_unchanged(registry):
    P.register_source("dexscreener", lambda c, a: _ds(1.0, LOW), primary=True)
    assert P.quote("robinhood", TOKEN, use_cache=False).confidence == LOW


def test_an_l2_weth_without_a_local_grade_takes_mainnet_eth(registry):
    op_weth = chains.get("optimism").wrapped_native

    def ds(chain, addr):
        if chain == "ethereum":
            return SourceAnswer(name="dexscreener", usd=2434.0, liquidity_usd=2e8,
                                priced_liquidity_usd=2e8, pool="0xp", pool_count=9,
                                confidence=HIGH)
        return SourceAnswer(name="dexscreener", error="no answer")
    P.register_source("dexscreener", ds, primary=True)
    q = P.quote("optimism", op_weth, use_cache=False)
    assert q.confidence == HIGH and q.primary_usd == 2434.0
    assert "ETH on ethereum" in q.reason


# -- the seat ------------------------------------------------------------------

class _Gate:
    per_tx_cap_usd = 6000.0
    daily_cap_usd = 12000.0

    def rolling_24h_spend_usd(self, venue=None):
        return 0.0


class _Tool:
    """A transfer whose value is NOT VERIFIED (the guard's quote marker)."""

    def __init__(self, value="$25.0010"):
        self.calls = []
        self.value = value

    def _get_wallet(self):
        return SimpleNamespace(policy=_Gate())

    async def transfer(self, params, ctx):
        self.calls.append(params)
        if params.dry_run:
            body = (f"transfer\n  simulated value: {self.value}\n"
                    f"  value check: {UNVERIFIED_MARK} — low-confidence\n"
                    "  guard: authorized\n  RESULT: DRY RUN (simulation only)")
        else:
            body = "transfer\n  RESULT: SENT AND CONFIRMED\n  tx: 0xabc"
        return SimpleNamespace(error=None, extracted_content=body)


ORDER = ["25", USDG, "to", TO, "on", "robinhood"]


def _send(args, tool):
    return asyncio.run(send_ops.send_reply("owner", args, tool=tool))


def test_an_unverified_quote_asks_for_the_extra_step_and_offers_no_go():
    tool = _Tool()
    out = _send(ORDER, tool)
    assert "EXTRA CHECK" in out
    assert out.rstrip().endswith(" ".join(["/send"] + ORDER + ["anyway"]))
    assert " go" not in out.splitlines()[-1]
    assert [p.dry_run for p in tool.calls] == [True]


def test_go_without_anyway_sends_nothing():
    tool = _Tool()
    out = _send(ORDER + ["go"], tool)
    assert out.startswith("❌ Nothing was sent")
    assert [p.dry_run for p in tool.calls] == [True]


def test_anyway_quote_then_anyway_go_sends_with_the_ack():
    tool = _Tool()
    quote = _send(ORDER + ["anyway"], tool)
    assert quote.rstrip().endswith("go") and "anyway" in quote.splitlines()[-1]
    out = _send(ORDER + ["anyway", "go"], tool)
    live = [p for p in tool.calls if not p.dry_run]
    assert len(live) == 1 and live[0].send_anyway is True
    assert "SENT AND CONFIRMED" in out


def test_an_unpriced_anyway_send_is_held_to_the_per_tx_cap():
    tool = _Tool(value="unknown")
    _send(ORDER + ["anyway", "go"], tool)
    live = [p for p in tool.calls if not p.dry_run]
    assert len(live) == 1 and live[0].max_spend_usd == 6000.0


# -- the card ------------------------------------------------------------------

@pytest.fixture
def card_store(tmp_path):
    st = cards.CardStore(str(tmp_path / "cards.db"))
    yield st


def test_the_unverified_quote_becomes_an_extra_check_card(card_store):
    reply = asyncio.run(send_ops.send_reply("owner", ORDER, tool=_Tool()))
    text, card = cards.quote_card("owner", "/send", ORDER, reply, st=card_store)
    assert card is not None and card.kind == cards.KIND_PROPOSAL
    assert card.args == ORDER + ["anyway"]                  # typed words only
    labels = [a.label for a in cards.card_actions(card)]
    assert labels[0] == "I understand — continue"
    assert "To continue:" not in card.body
    press = cards.press(card.card_id, "re", "owner", st=card_store)
    assert press.run == " ".join(["/send"] + ORDER + ["anyway"])   # a quote, never go


def test_the_anyway_quote_becomes_a_normal_confirm_card(card_store):
    reply = asyncio.run(send_ops.send_reply("owner", ORDER + ["anyway"], tool=_Tool()))
    text, card = cards.quote_card("owner", "/send", ORDER + ["anyway"], reply, st=card_store)
    assert card is not None and card.kind == cards.KIND_QUOTE
    assert card.confirm_line.startswith(" ".join(["/send"] + ORDER + ["anyway"]))
    assert card.confirm_line.endswith(" go")


# -- the owner thread ----------------------------------------------------------

def test_the_delta_carries_this_sessions_verb_replies(tmp_path):
    from core.surfaces.owner_thread import OwnerThreadStore
    st = OwnerThreadStore(str(tmp_path / "owner_thread.db"))
    st.record("out", "u", "transfer … RESULT: NOT SENT", session_id="s1",
              source="owner_verb", now=100.0)
    st.record("out", "u", "the agent's own reply", session_id="s1", source="reply",
              now=101.0)
    st.record("out", "u", "a cron line", session_id="s2", source="cron", now=102.0)
    bodies = [r["body"] for r in st.rows_since("u", since_ts=50.0, exclude_session_id="s1")]
    assert bodies == ["transfer … RESULT: NOT SENT", "a cron line"]


# -- the proposal pre-check -----------------------------------------------------

def test_a_send_proposal_that_cannot_parse_makes_no_card():
    from tools.controller.card_actions import _send_precheck
    assert "ticker" in _send_precheck(["/send", "25", "USDG", "to", TO, "on", "robinhood"])
    assert _send_precheck(["/send"] + ORDER) is None
    assert _send_precheck(["/swap", "1", "native"]) is None
