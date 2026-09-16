"""tx_guard's pause refusal must be as honest as the bridge's.

Census, 2026-09-12. Commit `125f83dd` fixed the "autonomy is HALTED (owner
kill-switch)" wording in `tools/defi/bridge_verb.py` only. `core/wallet/tx_guard.py`
kept the identical string at its first gate, so every OTHER money verb -- swap,
solana_swap, transfer, approve_token, revoke_approval -- still sent the owner
hunting for a lever that does not exist, with no remedy offered at all.

`AutonomyConfig.autonomy_halted()` is literally `not allows("dispatch").allowed`:
a facet of the 031 pause record, not a second switch. Both verbs now render one
sentence from `core.autonomy_control.pause_refusal_text`, so they cannot drift
into two explanations of one stop.
"""
import pytest

from core.wallet import tx_guard


def _halt_refusal():
    """The Decision tx_guard returns when the owner's stop is in force.

    `halted_fn` is injected rather than monkeypatched so this test never touches
    a pause record -- `autonomy_control.pause()` writes to EVERY probed base, and
    a careless call here would pause the developer's real data home.
    """
    return tx_guard.authorize(
        intent=None, tx={}, holder="0x0", gate=None,
        tool_self=None, execution_context=None,
        halted_fn=lambda: True)


def test_the_refusal_does_not_blame_a_phantom_kill_switch():
    text = _halt_refusal().reason.lower()
    assert "(owner kill-switch)" not in text
    assert "autonomy is halted" not in text
    assert "not a separate kill-switch" in text


def test_the_refusal_names_the_pause_as_the_cause():
    assert "pause" in _halt_refusal().reason.lower()


def test_the_refusal_offers_a_remedy_that_exists():
    """The old branch offered none. `/resume` and `polyrob autonomy resume` are
    both real seats for the same record."""
    text = _halt_refusal().reason
    assert "/resume" in text or "autonomy resume" in text


def test_the_refusal_says_it_binds_the_owners_own_seat():
    """The gate runs before any seat distinction, so telling the owner to type the
    command himself -- as the agent did live on 2026-09-12 -- is wrong advice."""
    assert "own seat" in _halt_refusal().reason.lower()


def test_the_refusal_states_nothing_was_broadcast():
    """A money-path refusal that leaves the owner unsure whether it sent is the
    one that tempts a re-send."""
    assert "Nothing was broadcast" in _halt_refusal().reason


def test_a_probe_that_raises_still_refuses():
    """Fail-closed: an unreadable stop is a stop."""
    def _boom():
        raise RuntimeError("probe down")
    d = tx_guard.authorize(intent=None, tx={}, holder="0x0", gate=None,
                           tool_self=None, execution_context=None,
                           halted_fn=_boom)
    assert d.allowed is False
    assert "failing closed" in d.reason
