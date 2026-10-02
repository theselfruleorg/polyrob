"""CLI10 (2026-10-03 audit): a tap token, `/run` and `/cards` reach the dispatcher
during a turn.

The REPL refused them as "turn in progress": a tapped `/approve_tap_<id>`,
`/fulfill_<ask>_<letter>` or `/card_<id>_ok` decides the owner queue and never
replaces the conversation, so it is a live command like `/approve` itself.
"""
import pytest

from cli.ui.input_policy import is_live_command


@pytest.mark.parametrize("line", [
    "/approve_tap_abc123", "/approve_p_1245c6", "/approve_all", "/reject_p_1245c6",
    "/fulfill_0123abcd_a", "/run", "/run stop 2", "/cards",
])
def test_live_during_a_turn(line):
    assert is_live_command(line)


def test_card_token_is_live():
    from core.surfaces.cards import card_token
    assert is_live_command(card_token("ab12cd34ef", "ok"))


@pytest.mark.parametrize("line", ["/model gpt", "/new", "/approve_tap_", "approve_all",
                                  "/approve_bad-sep"])
def test_still_refused(line):
    assert not is_live_command(line)
