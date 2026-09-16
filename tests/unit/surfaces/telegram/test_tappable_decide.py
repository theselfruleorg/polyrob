"""One tap, no typing (owner complaint, 2026-09-12).

Verbatim: "the whole command should be highlighted so i could tap on it. now i
need comy and type in the id of approve".

Telegram auto-links a `/word` token of [A-Za-z0-9_] (max 32 chars) and sends the
WHOLE token on tap. It does NOT link a trailing argument — so `/approve tap-abc`
rendered only `/approve` as tappable, which was the half that did nothing, and
the id had to be copied by hand off a phone.
"""
import pytest

from surfaces.telegram.harness import normalize_tappable_command as norm


def test_the_one_token_form_maps_back_to_verb_and_id():
    assert norm("/approve_tap_dd2db0417c63") == ("/approve", "tap-dd2db0417c63")
    assert norm("/reject_tap_abc123") == ("/reject", "tap-abc123")


def test_it_is_case_insensitive_on_the_verb_but_not_the_id():
    verb, tap = norm("/Approve_tap_AbC123")
    assert verb == "/approve"
    assert tap == "tap-AbC123", "an id is data; folding its case could miss it"


def test_the_spaced_form_is_left_alone():
    """It still works — it is just not what a phone taps."""
    assert norm("/approve tap-dd2db0417c63") == (None, None)
    assert norm("/approve") == (None, None)


def test_a_non_tap_id_is_REFUSED_rather_than_guessed():
    """A skill or correspondent id carries its own separators, so mapping every
    `_` back to `-` would act on the WRONG item. On a queue that holds money
    approvals, acting on the wrong item is the one outcome worth refusing for."""
    assert norm("/approve_skill_treasury_bridge_route") == (None, None)
    assert norm("/approve_telegram_12345") == (None, None)


def test_ordinary_text_is_not_a_command():
    for text in ("hello", "", "   ", "/approveall", "approve_tap_x", "/help"):
        assert norm(text) == (None, None), text


def test_the_token_fits_telegram_s_32_char_command_limit():
    """Beyond 32 chars Telegram stops auto-linking, and the tap silently becomes
    a copy job again."""
    token = "approve_" + "tap-dd2db0417c63".replace("-", "_")
    assert len(token) <= 32, len(token)


def test_the_card_offers_the_tappable_form_first():
    from tools.controller.grant_card import render_grant_card
    card = render_grant_card("defi_trade_bridge", {"amount": 0.036},
                             "tap-dd2db0417c63", timeout_sec=300)
    assert "/approve_tap_dd2db0417c63" in card
    assert "/reject_tap_dd2db0417c63" in card
    # and the spaced form survives for every other seat
    assert "/approve tap-dd2db0417c63" in card
    # the tappable line must come FIRST — it is the one a phone can use
    assert card.index("/approve_tap_") < card.index("/approve tap-")


def test_the_rendered_form_round_trips_through_the_parser():
    """The card and the parser must agree, or the tap is a dead link."""
    from tools.controller.grant_card import render_grant_card
    display = "tap-9f2c1b0a7e64"
    card = render_grant_card("defi_trade_swap", {"amount": 1}, display)
    token = next(w for line in card.splitlines() for w in line.split()
                 if w.startswith("/approve_"))
    assert norm(token) == ("/approve", display)
