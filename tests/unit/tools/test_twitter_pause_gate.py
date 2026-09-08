"""033 T0.3 — `/pause social` must actually stop autonomous posting.

`allows("social_post")` was declared in core/autonomy_control.py:63 on
2026-09-03 and called from NOWHERE: no outbound write tool consulted the pause
predicate at all, so the owner could pause social posting and the agent kept
posting. This is its first consumer.
"""
import types

import pytest


class _Forged:
    """role='leaf' is what makes _is_forged_or_autonomous_turn answer True."""
    user_id = "u1"
    session_id = "s1"
    role = "leaf"
    is_sub_agent = False
    metadata = {"turn_kind": "cron"}


class _Owner:
    user_id = "u1"
    session_id = "s1"
    role = "orchestrator"
    is_sub_agent = False
    metadata = {}


def _tool():
    from tools.twitter_tool import TwitterTool
    t = TwitterTool.__new__(TwitterTool)
    t.logger = types.SimpleNamespace(warning=lambda *a, **k: None,
                                     debug=lambda *a, **k: None,
                                     info=lambda *a, **k: None)
    return t


@pytest.fixture
def isolated_pause(tmp_path, monkeypatch):
    """⚠️ Never call ac.pause() without isolating state_bases — it probes
    POLYROB_DATA_DIR / DATA_ROOT / the resolved data home and would pause the
    developer's REAL data home."""
    import core.autonomy_control as ac
    monkeypatch.setattr(ac, "state_bases", lambda d=None: [str(tmp_path)])
    return ac


def test_autonomous_post_is_refused_while_social_is_paused(isolated_pause):
    isolated_pause.pause(scopes=("social",), set_by="test", via="unit")
    block = _tool()._pause_block("twitter_post", _Forged())
    assert block is not None
    assert "paused" in block.lower()
    assert "social" in block.lower()


def test_the_gate_covers_every_write_verb_not_just_the_cooldown_pair(isolated_pause):
    """_SOCIAL_COOLDOWN_ACTIONS is {twitter_post, twitter_thread}; the pause gate
    must not inherit that gap — twitter_reply and twitter_quote are public posts."""
    isolated_pause.pause(scopes=("social",), set_by="test", via="unit")
    tool = _tool()
    for verb in ("twitter_reply", "twitter_quote", "twitter_dm", "twitter_retweet"):
        assert tool._pause_block(verb, _Forged()) is not None, verb


def test_a_full_pause_also_stops_posting(isolated_pause):
    isolated_pause.pause(scopes=("all",), set_by="test", via="unit")
    assert _tool()._pause_block("twitter_post", _Forged()) is not None


def test_an_unrelated_scope_does_not_stop_posting(isolated_pause):
    isolated_pause.pause(scopes=("trading",), set_by="test", via="unit")
    assert _tool()._pause_block("twitter_post", _Forged()) is None


def test_owner_turn_is_never_pause_gated(isolated_pause, monkeypatch):
    """Asking the agent to post IS the owner being in the loop."""
    isolated_pause.pause(scopes=("social",), set_by="test", via="unit")
    monkeypatch.setattr(
        "tools.controller.action_registration._is_forged_or_autonomous_turn",
        lambda ctx, s: False)
    assert _tool()._pause_block("twitter_post", _Owner()) is None


def test_no_context_is_never_gated(isolated_pause):
    """A CLI / programmatic call carries no execution context."""
    isolated_pause.pause(scopes=("all",), set_by="test", via="unit")
    assert _tool()._pause_block("twitter_post", None) is None


def test_unpaused_is_a_no_op(isolated_pause):
    assert _tool()._pause_block("twitter_post", _Forged()) is None
