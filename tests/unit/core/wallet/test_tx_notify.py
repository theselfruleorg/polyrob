"""The money-execution notice rail (039 Unit A).

The failure this replaces is on the record. On 2026-09-12 the only automatic
message the owner received about a live bridge was the tiered spend-lane hook
reusing the RECEIVE lane's text — "Auto-approved payment request
(defi_trade_bridge) (within caps; PAYMENT_APPROVAL_MODE=auto)" — about a
transaction he had approved by hand twice, with no chain, no amount, no hash and
no arrival in it.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.wallet import tx_notify as tn


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Rendering: unknown is `unknown`, never a comfortable zero
# --------------------------------------------------------------------------

def test_a_broadcast_notice_carries_what_left_and_how_to_follow_it():
    text = tn.render_broadcast(tn.TxNotice(
        verb="bridge", route="solana→base", amount_in="0.93 SOL",
        amount_out="≥0.03640 ETH", usd=94.87,
        tx_ref="5xQkabcdef1234567890deadbeef9ab",
        extra_refs=("0x7c11223344556677",), lane="owner-approved",
        cap_used_usd=94.87, cap_limit_usd=250.0))
    assert "⛓ SENT · bridge solana→base" in text
    assert "0.93 SOL ($94.87) → ≥0.03640 ETH" in text
    assert "tx 5xQkab…f9ab" in text
    assert "ref 0x7c11…6677" in text
    assert "cap: $94.87 of $250.00 daily" in text
    assert "lane owner-approved" in text


def test_an_unknown_usd_renders_unknown_never_zero():
    """A $0.00 in a money report is indistinguishable from a free transaction.
    That confident zero is what let 114 tenantless wallet_spend rows read as a
    clean ledger."""
    text = tn.render_broadcast(tn.TxNotice(
        verb="swap", route="base", amount_in="1 WETH", usd=None, tx_ref="0xabc"))
    assert "unknown" in text
    assert "$0.00" not in text


def test_an_arrival_notice_leads_with_the_MEASURED_number():
    text = tn.render_settled(tn.TxNotice(
        verb="bridge", route="solana→base", tx_ref="5xQk1234567890abcd",
        extra_refs=("e6c25b8055794241",), state=tn.STATE_ARRIVED,
        measured="+0.03716593 ETH on base", detail="floor 0.0364 · 41s",
        ledger_recorded=True))
    assert text.startswith("✅ ARRIVED · bridge solana→base")
    assert "measured +0.03716593 ETH on base" in text


def test_a_quoted_number_is_never_printed_as_a_measured_one():
    """Without a measurement, saying the quoted figure would report a third
    party's claim about our money as a fact about it."""
    text = tn.render_settled(tn.TxNotice(
        verb="bridge", route="base→robinhood", tx_ref="0xabc",
        amount_out="≥0.0359 ETH", state=tn.STATE_IN_FLIGHT))
    assert "not independently measured" in text
    assert "measured " not in text.replace("not independently measured", "")


def test_in_flight_is_never_reported_as_a_failure():
    """A re-sent bridge pays twice, so the notice must not read like one to
    retry."""
    text = tn.render_settled(tn.TxNotice(
        verb="bridge", route="solana→base", tx_ref="0xabc",
        state=tn.STATE_IN_FLIGHT))
    assert "⏳" in text
    assert "not a failure" in text.lower()
    assert "Do NOT re-send" in text


def test_a_reverted_notice_says_the_fee_was_spent():
    text = tn.render_settled(tn.TxNotice(verb="swap", route="base",
                                         tx_ref="0xabc", state=tn.STATE_REVERTED))
    assert "❌" in text
    assert "fee was spent" in text


def test_a_missing_ledger_record_is_shouted_not_hidden():
    """An unrecorded spend is invisible to every other money verb's cap."""
    text = tn.render_settled(tn.TxNotice(
        verb="swap", route="base", tx_ref="0xabc", state=tn.STATE_CONFIRMED,
        ledger_recorded=False))
    assert "NOT recorded" in text


def test_short_never_elides_a_ref_into_uselessness():
    assert tn.short("0xabc") == "0xabc"
    assert tn.short(None) == "unknown"
    assert tn.short("") == "unknown"
    long = "0x" + "a" * 40
    assert tn.short(long).startswith("0xaaaa") and tn.short(long).endswith("aaaa")


# --------------------------------------------------------------------------
# Explorer link (043 A38/A5) — a `chain` on the notice adds a link, and its
# absence (every pre-existing call site) is byte-identical to before the
# field existed.
# --------------------------------------------------------------------------

def test_a_broadcast_notice_with_a_chain_carries_the_explorer_link():
    text = tn.render_broadcast(tn.TxNotice(
        verb="swap", route="base", amount_in="1 WETH", usd=100.0,
        tx_ref="0xabc123", chain="base"))
    assert "https://basescan.org/tx/0xabc123" in text


def test_a_settled_notice_with_a_chain_carries_the_explorer_link():
    text = tn.render_settled(tn.TxNotice(
        verb="swap", route="base", tx_ref="0xabc123", chain="base",
        state=tn.STATE_CONFIRMED))
    assert "https://basescan.org/tx/0xabc123" in text


def test_no_chain_means_no_link_byte_identical_to_before():
    text = tn.render_broadcast(tn.TxNotice(
        verb="swap", route="base", amount_in="1 WETH", usd=100.0, tx_ref="0xabc123"))
    assert "basescan" not in text
    assert "http" not in text


def test_an_unknown_chain_omits_the_link_rather_than_raising():
    text = tn.render_broadcast(tn.TxNotice(
        verb="swap", route="nochain", amount_in="1 X", usd=1.0,
        tx_ref="0xabc", chain="nochain"))
    assert "http" not in text


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------

def test_the_notice_rides_the_critical_lane():
    """Over 8 days in August the shared daily cap dropped 195 of 196 owner
    notices. Money that already moved may not queue behind '▶ goal started'."""
    from core.surfaces.user_delivery import _CRITICAL_SOURCES, resolve_priority
    assert tn.SOURCE in _CRITICAL_SOURCES
    assert resolve_priority(tn.SOURCE, None) == "critical"


def test_delivery_passes_the_source_and_critical_priority(monkeypatch):
    seen = {}

    async def _deliver(container, user_id, text, **kw):
        seen.update(kw)
        seen["text"] = text
        seen["user_id"] = user_id
        return "sent"
    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    out = _run(tn.notify(None, "owner", tn.TxNotice(verb="swap", route="base",
                                                    tx_ref="0xabc"), settled=False))
    assert out == "sent"
    assert seen["source"] == tn.SOURCE
    assert seen["priority"] == "critical"
    assert seen["user_id"] == "owner"


def test_a_delivery_failure_never_raises(monkeypatch):
    """A notification failure must not turn a settled transaction into an error."""
    async def _boom(*a, **kw):
        raise RuntimeError("telegram down")
    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _boom)
    assert _run(tn.notify(None, "owner", tn.TxNotice(verb="swap", route="base"),
                          settled=True)) == "error"


def test_a_tenantless_notice_is_recorded_but_never_cross_delivered(monkeypatch):
    """Inventing an owner for a notice with no tenant would cross tenants."""
    called = []

    async def _deliver(*a, **kw):
        called.append(a)
        return "sent"
    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    assert _run(tn.notify(None, "", tn.TxNotice(verb="swap", route="base"),
                          settled=False)) == "error"
    assert not called


def test_the_flag_off_delivers_nothing(monkeypatch):
    monkeypatch.setenv("TX_NOTIFY_ENABLED", "false")
    called = []

    async def _deliver(*a, **kw):
        called.append(a)
        return "sent"
    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    assert _run(tn.notify(None, "owner", tn.TxNotice(verb="swap", route="base"),
                          settled=False)) == "skipped"
    assert not called


def test_notify_soon_holds_a_reference_to_its_task(monkeypatch):
    """A task with no strong reference can be garbage collected mid-flight —
    which would drop the notice silently, the exact failure this rail ends."""
    sent = []

    async def _deliver(container, user_id, text, **kw):
        sent.append(text)
        return "sent"
    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)

    async def _drive():
        tn.notify_soon(None, "owner", tn.TxNotice(verb="swap", route="base",
                                                  tx_ref="0xabc"), settled=False)
        assert tn._PENDING, "the task must be referenced while it runs"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.run(_drive())
    assert sent and "⛓ SENT" in sent[0]


# --------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------

def test_caps_come_from_the_gates_public_readers():
    from core.wallet.policy import PolicyGate
    gate = PolicyGate(max_per_tx_usd=50.0, daily_cap_usd=250.0)
    gate.record(venue="defi", action="swap", amount_usd=10.0,
                counterparty="0x1", idempotency_key="a", result_ref="0x2")
    used, limit = tn.caps_from_gate(gate)
    assert used == pytest.approx(10.0)
    assert limit == pytest.approx(250.0)


def test_an_unreadable_cap_is_omitted_not_rendered_as_zero():
    class _Broken:
        @property
        def daily_cap_usd(self):
            raise RuntimeError("nope")
    used, limit = tn.caps_from_gate(_Broken())
    assert (used, limit) == (None, None)
    text = tn.render_broadcast(tn.TxNotice(verb="swap", route="base", tx_ref="0x1",
                                           cap_used_usd=used, cap_limit_usd=limit))
    assert "cap:" not in text


def test_no_daily_cap_configured_reads_as_unknown_not_zero():
    from core.wallet.policy import PolicyGate
    assert tn.caps_from_gate(PolicyGate(max_per_tx_usd=50.0)) == (None, None)


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

def test_the_event_is_recorded_even_when_delivery_fails(monkeypatch):
    """A notice the owner never received must still be reconstructable."""
    events = []
    monkeypatch.setattr("core.event_log.event_log_enabled", lambda: True)
    monkeypatch.setattr("core.event_log.get_event_log",
                        lambda: SimpleNamespace(
                            record=lambda kind, **kw: events.append((kind, kw))))

    async def _boom(*a, **kw):
        raise RuntimeError("down")
    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _boom)
    _run(tn.notify(None, "owner", tn.TxNotice(verb="bridge", route="solana→base",
                                              tx_ref="0xabc", usd=94.87),
                   settled=False))
    assert events and events[0][0] == "tx_broadcast"
    assert events[0][1]["attrs"]["usd"] == 94.87


def test_both_event_kinds_are_registered():
    from core.event_kinds import KNOWN_KINDS, TX_BROADCAST, TX_SETTLED
    assert TX_BROADCAST in KNOWN_KINDS and TX_SETTLED in KNOWN_KINDS
