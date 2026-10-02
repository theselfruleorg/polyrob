"""A refusal must name what actually refused, and what lifts it.

Live, 2026-09-12. The owner armed `DEFI_AGENT_AUTONOMY=true` so the agent
genuinely held the bridge verb, then asked it in chat to bridge. It refused and
told him the refusal was a hard safety gate it could not self-grant, then told
him to type `/bridge` himself instead.

Both halves were wrong. The tool WAS in its toolset. What refused was his OWN
`/pause all` from that morning — and typing `/bridge` would have hit the
identical refusal, because `perform_bridge` checks the pause regardless of seat.

The message he saw was "refused: autonomy is HALTED (owner kill-switch)". He
never touched a kill-switch; he typed `/pause`. `AutonomyConfig.autonomy_halted`
is literally `not allows("dispatch").allowed` — a facet of the same pause record
— so naming it a separate lever sends the owner looking for a switch that does
not exist, and that branch offered no remedy at all.
"""
import pytest


def _refusal(monkeypatch, *, halted=True):
    import tools.defi.bridge_verb as bv
    from core.config_policy import AutonomyConfig
    monkeypatch.setattr(AutonomyConfig, "autonomy_halted", staticmethod(lambda: halted))
    return bv._refuse_paused()


def test_a_pause_refusal_does_not_blame_a_kill_switch(monkeypatch):
    """The owner typed /pause. Naming a separate 'kill-switch' as the CAUSE sends
    him looking for a lever that does not exist — `autonomy_halted` is a facet of
    the same record. Saying "this is NOT a kill-switch" is the correction, so the
    test targets the claim, not the word."""
    text = (_refusal(monkeypatch) or "").lower()
    assert "(owner kill-switch)" not in text
    assert "autonomy is halted" not in text
    assert "not a separate kill-switch" in text


def test_the_refusal_names_the_pause_as_the_cause(monkeypatch):
    text = (_refusal(monkeypatch) or "").lower()
    assert "pause" in text


def test_the_refusal_never_says_spending_is_running(monkeypatch):
    """`allows("spend")` can read ALLOWED with an empty reason when `halted` is
    what tripped; interpolating it printed "spending is running", which is the
    opposite of what happened."""
    assert "is running" not in (_refusal(monkeypatch) or "")


def test_a_pause_refusal_names_the_remedy(monkeypatch):
    """A refusal with no remedy is where the owner gets stuck."""
    text = (_refusal(monkeypatch) or "")
    assert "/resume" in text or "resume" in text.lower()


def test_a_pause_refusal_says_the_owner_is_not_bound(monkeypatch):
    """2026-09-26: the pause stops the agent's own work; the owner's own
    `/bridge … go` passes it (owner_direct_turn). The refusal must say so, so
    the agent points the owner at his own verb instead of at /resume."""
    text = (_refusal(monkeypatch) or "").lower()
    assert "not bound" in text


def test_nothing_refuses_when_not_paused(monkeypatch):
    import core.autonomy_control as ac
    from types import SimpleNamespace
    monkeypatch.setattr(ac, "allows", lambda kind, d=None: SimpleNamespace(allowed=True, reason=""))
    assert _refusal(monkeypatch, halted=False) is None


def test_the_refusal_still_states_nothing_was_broadcast(monkeypatch):
    """The money-safety invariant: every bridge refusal says so explicitly."""
    assert "Nothing was broadcast" in (_refusal(monkeypatch) or "")
