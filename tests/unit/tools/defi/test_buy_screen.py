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
