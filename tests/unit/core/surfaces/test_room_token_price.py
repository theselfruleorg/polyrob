"""A room may price an action in TOKENS, not dollars.

⚠️ Why this exists. Every room price was USD (`chat.paid_mute_usd`), so a
non-stable asset had to be QUOTED, so it had to clear `pool_screen.classify`.
A room selling in the house's own token was therefore hostage to a *trading*
heuristic answering a different question — "should I buy this?" — and a
36-hour-old launch with ordinary churn (V/L 14.6 against a threshold of 10)
could not be charged in at all. Measured on PNL/Robinhood Chain, 2026-09-15.

A token-denominated price removes the oracle from the path entirely: the
amount IS the price, so there is nothing to quote and nothing to screen.

⚠️ The USD figure is still REQUIRED and still recorded. It is the owner's
declared value, and it is what the ledger, `X402_INVOICE_MAX_USD` and the
owner's own reporting read. Booking a token-priced offer at $0.00 would be the
confident-zero this codebase keeps having to fix.
"""
import pytest

from core.surfaces import chat_policy
from core.surfaces.room_actions import offer


# --- async-seam shim (2026-09-15) -------------------------------------------
# `offer()` became `async def` so a Telegram read runs on the CALLER's event
# loop rather than a bridged one ("got Future attached to a different loop").
# These tests drive it synchronously.
import asyncio as _aio
from core.surfaces import room_actions as _ra


def _sync_seam(fn):
    def _call(*a, **k):
        r = fn(*a, **k)
        return _aio.run(r) if _aio.iscoroutine(r) else r
    return _call


offer = _sync_seam(_ra.offer)



@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ROOM_ACTIONS_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "5150")
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    monkeypatch.delenv("PAYMENT_DEFAULT_ASSET", raising=False)
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: "rob")


class _Cfg:
    def __init__(self, d):
        self.data_dir = d


class _Container:
    def __init__(self, d):
        self.config = _Cfg(d)

    def get_service(self, name):
        raise KeyError(name)


CHAT = "-100777"


@pytest.fixture
def setup(tmp_path):
    """An allowlisted room selling a mute, priced in a volatile token."""
    home = str(tmp_path)
    from core.payments.assets import AssetStore, PaymentAsset, store_path
    AssetStore(store_path(home)).upsert(PaymentAsset(
        asset_id="pnl", chain="robinhood", address="0x" + "bb" * 20,
        decimals=18, symbol="PNL", rail="onchain_scan",
        min_amount_raw=500 * 10 ** 18, liquidity_floor_usd=5000.0,
        source="operator"))
    import os
    from core.surfaces.group_allowlist import GroupAllowlist
    GroupAllowlist(os.path.join(home, "group_allowlist.db")).allow(
        "telegram", CHAT, note="t")
    for k, v in (("chat.paid_enabled", "true"), ("chat.paid_asset", "pnl"),
                 ("chat.paid_mute_usd", "0.10"),
                 ("chat.paid_mute_max_duration", "6h"),
                 ("chat.member_verbs", "mute")):
        ok, msg = chat_policy.set(home, "rob", "telegram", CHAT, k, v)
        assert ok, msg
    return home


def _mint(**kw):
    """A minter that records what it was asked for."""
    calls = {}

    def mint(**k):
        calls.update(k)
        return {"request_id": "inv_x", "amount_raw": k["amount_raw"]}

    mint.calls = calls
    return mint


class _WashQuoter:
    """The live PNL situation: a real price over a pool the screen calls WASH."""

    def quote(self, asset):
        import time
        from core.payments.quote import PriceQuote
        return PriceQuote(asset_id=asset.asset_id, usd_per_token=0.00006988,
                          liquidity_usd=19838.0, verdict="WASH",
                          source="test", ts=time.time())


def _offer(home, **over):
    mint = _mint()
    kw = dict(surface="telegram", chat_id=CHAT, verb="mute",
              target_user_id="777", requester_id="888", target_name="t",
              duration="1h", mint_fn=mint, quoter=_WashQuoter())
    kw.update(over)
    return offer(_Container(home), **kw), mint


# --- the block this removes ------------------------------------------------

def test_a_usd_priced_room_is_still_blocked_by_a_wash_pool(setup):
    """The existing behaviour, unchanged — this is what we are working around."""
    r, _ = _offer(setup)
    assert r.ok is False
    assert r.reason_code == "unpriceable"


# --- the token price -------------------------------------------------------

def test_a_token_price_needs_no_quote_at_all(setup):
    ok, msg = chat_policy.set(setup, "rob", "telegram", CHAT,
                              "chat.paid_mute_units", "1500 pnl")
    assert ok, msg
    r, mint = _offer(setup)
    assert r.ok is True, getattr(r, "text", "")
    assert mint.calls["amount_raw"] == 1500 * 10 ** 18


def test_a_token_price_works_even_with_no_quoter_present(setup):
    chat_policy.set(setup, "rob", "telegram", CHAT, "chat.paid_mute_units", "1500 pnl")
    r, mint = _offer(setup, quoter=None)
    assert r.ok is True
    assert mint.calls["amount_raw"] == 1500 * 10 ** 18


def test_a_fractional_token_price_is_exact(setup):
    """⚠️ Decimal, not float: 0.1 tokens at 18 decimals must not drift."""
    chat_policy.set(setup, "rob", "telegram", CHAT,
                    "chat.paid_mute_units", "1500.5 pnl")
    r, mint = _offer(setup)
    assert r.ok is True
    assert mint.calls["amount_raw"] == 15005 * 10 ** 17


def test_the_declared_usd_is_still_recorded(setup):
    """⚠️ Never $0.00. The ledger, the invoice cap and the owner's reporting
    all read this figure."""
    chat_policy.set(setup, "rob", "telegram", CHAT, "chat.paid_mute_units", "1500 pnl")
    r, mint = _offer(setup)
    assert r.ok is True
    assert mint.calls["price_usd"] == pytest.approx(0.10)


def test_the_asset_token_floor_still_applies(setup):
    """The real anti-dust control is not bypassed by pricing in tokens."""
    chat_policy.set(setup, "rob", "telegram", CHAT, "chat.paid_mute_units", "10 pnl")
    r, _ = _offer(setup)
    assert r.ok is False
    assert r.reason_code == "unpriceable"


def test_a_zero_or_negative_token_price_is_refused(setup):
    for bad in ("0", "-5"):
        chat_policy.set(setup, "rob", "telegram", CHAT,
                        "chat.paid_mute_units", f"{bad} pnl")
        r, _ = _offer(setup)
        assert r.ok is False, f"{bad} was accepted"


def test_the_offer_text_names_the_token_amount(setup):
    chat_policy.set(setup, "rob", "telegram", CHAT, "chat.paid_mute_units", "1500 pnl")
    r, _ = _offer(setup)
    assert "1,500 PNL" in r.text or "1500 PNL" in r.text


def test_a_room_with_no_token_price_is_unchanged(setup):
    """Byte-identical legacy path: USD price, quote, screen."""
    r, _ = _offer(setup)
    assert r.ok is False and r.reason_code == "unpriceable"


def test_a_dollar_pegged_asset_ignores_a_token_price_absence(setup, tmp_path):
    """USDC still sizes from USD as before."""
    chat_policy.set(setup, "rob", "telegram", CHAT, "chat.paid_asset", "usdc-base")
    r, mint = _offer(setup, quoter=None)
    assert r.ok is True
    assert mint.calls["amount_raw"] == 100_000       # 0.10 * 10**6


# --- what a MEMBER is quoted ----------------------------------------------

def test_the_member_price_list_quotes_the_token_amount(setup):
    """⚠️ A member quoted '$0.10' while actually sending 1,500 PNL is a lie in
    the one place the payer reads before paying."""
    from core.surfaces.room_actions import render_member_prices
    chat_policy.set(setup, "rob", "telegram", CHAT, "chat.paid_mute_units", "1500 pnl")
    out = render_member_prices(_Container(setup), surface="telegram", chat_id=CHAT)
    assert "1,500 PNL" in out


def test_a_token_price_without_a_declared_usd_names_the_remedy(setup):
    """The USD figure is the ledger's; a room that drops it must be TOLD, not
    silently reported as unpriced."""
    from core.surfaces.room_actions import render_member_prices
    chat_policy.set(setup, "rob", "telegram", CHAT, "chat.paid_mute_units", "1500 pnl")
    chat_policy.set(setup, "rob", "telegram", CHAT, "chat.paid_mute_usd", "0")
    out = render_member_prices(_Container(setup), surface="telegram", chat_id=CHAT)
    assert "/paid price mute" in out
