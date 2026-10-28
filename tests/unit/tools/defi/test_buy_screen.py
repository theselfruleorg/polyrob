from types import SimpleNamespace

import pytest

from tools.defi import buy_screen, token_screen


def clean():
    return token_screen.Screen(checks=[
        token_screen.Check("is_honeypot", "0", "goplus"),
        token_screen.Check("sell_tax", "0", "goplus")])


def test_hard_failure_overrides_another_sources_clean_answer():
    screen = clean()
    screen.hard_fails.append("is_honeypot (honeypot.is)")
    assert buy_screen.screen_refusal(screen)


def test_unanswered_sell_check_is_not_a_pass():
    screen = clean()
    screen.checks.pop()
    assert "sell_tax" in buy_screen.screen_refusal(screen)
    assert buy_screen.screen_refusal(clean()) is None


@pytest.mark.asyncio
async def test_live_screen_merges_actual_provider_answers(monkeypatch):
    monkeypatch.setattr(buy_screen.goplus, "screen", lambda *a: None)
    monkeypatch.setattr(token_screen, "gather_facts", lambda *a: [])
    identity = SimpleNamespace(source="rpc")
    assert await buy_screen.evm_buy_refusal("base", "token", identity)
    identity.source = "canonical"
    assert await buy_screen.evm_buy_refusal("base", "token", identity) is None


def test_unknown_route_ceiling_applies_without_identity_exemption():
    assert buy_screen.unchecked_route_refusal("UNAVAILABLE", 6)
    assert buy_screen.unchecked_route_refusal("UNAVAILABLE", 5) is None
    assert buy_screen.unchecked_route_refusal("AGREES", 6) is None


def test_a_trusted_token_with_no_price_source_is_not_ticket_capped():
    # Prod 2026-10-10 12:25Z: the owner's rebuy of the instance's own launch
    # (TKN on robinhood, where no price source covers the chain) was refused at
    # $5 on every run. The owner approved lifting the ticket for a TRUSTED
    # token whose route is UNAVAILABLE; the per-tx ceiling, slippage and the
    # route floor still bound it.
    assert buy_screen.unchecked_route_refusal("UNAVAILABLE", 144, trusted=True) is None


def test_trust_never_lifts_a_disagreeing_route_or_an_untrusted_token():
    assert buy_screen.unchecked_route_refusal("DISAGREES", 144, trusted=True)
    assert buy_screen.unchecked_route_refusal("UNAVAILABLE", 144, trusted=False)


def test_the_refusal_names_the_owner_trust_verb():
    # Prod 2026-10-16 13:52Z: the refusal said "owner pin" without naming the
    # verb; the agent invented `/config set budget.unchecked_route_usd 300` (no
    # such key) and the owner's own /swap failed twice. Name the real lever.
    why = buy_screen.unchecked_route_refusal("UNAVAILABLE", 293)
    assert "/wallet trust <chain> <address> <SYMBOL> go" in why


def test_an_owner_pinned_token_passes_an_unanswered_check_only():
    # Prod 2026-10-16 13:58Z: GoPlus covers USDG on robinhood but never answers
    # is_honeypot, so every live buy of the owner-pinned contract was refused.
    # The owner's pin covers an UNANSWERED check; a hard failure or a high sell
    # tax still refuses.
    screen = clean()
    screen.checks = [c for c in screen.checks if c.name != "is_honeypot"]
    assert buy_screen.screen_refusal(screen)
    assert buy_screen.screen_refusal(screen, owner_pinned=True) is None
    hard = clean()
    hard.hard_fails.append("is_honeypot (goplus)")
    assert buy_screen.screen_refusal(hard, owner_pinned=True)
    taxed = clean()
    taxed.flags.append("sell_tax_high")
    assert buy_screen.screen_refusal(taxed, owner_pinned=True)


@pytest.mark.asyncio
async def test_live_screen_honours_the_owner_pin_for_unanswered_checks(monkeypatch):
    monkeypatch.setattr(buy_screen.goplus, "screen", lambda *a: None)
    monkeypatch.setattr(token_screen, "gather_facts", lambda *a: [])
    identity = SimpleNamespace(source="rpc")
    assert await buy_screen.evm_buy_refusal("robinhood", "t", identity)
    assert await buy_screen.evm_buy_refusal("robinhood", "t", identity,
                                            owner_pinned=True) is None
