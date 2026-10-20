"""A cron job's ``payload.post_spacing_hours`` is ENFORCED by the X tool.

2026-10-16 18:00Z: the X growth job (wakes every 6 h, "never post twice inside
12 h" in prose) quoted a tweet 6.0 h after its last post and only then noticed.
The spacing is now a refusal keyed on the job's OWN posts.
"""
import pytest

pytest.importorskip("polyrob_x")

from polyrob_x.twitter_tool import TwitterPostAction, TwitterQuoteAction

from core import autonomy_marker
from tests.packs.x.test_twitter_social_cooldown import _autonomous_ctx, _resp, _tool, _via


@pytest.fixture
def no_cooldown(monkeypatch):
    # Isolate the per-job spacing from the 1 h cross-session cooldown.
    monkeypatch.setenv("TWITTER_POST_COOLDOWN_ENABLED", "false")


def _run_of(job_id, sid, spacing=None):
    autonomy_marker.mark_autonomous(sid, None, cron_job_id=job_id)
    autonomy_marker.note_job(job_id, "growth", post_spacing_hours=spacing)
    return _autonomous_ctx(session_id=sid)


@pytest.mark.asyncio
async def test_second_post_by_the_same_job_inside_its_spacing_is_refused(monkeypatch, no_cooldown):
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = [_resp("1"), _resp("2")]
    first = await _via(t).twitter_post(TwitterPostAction(text="rung 1"),
                                       execution_context=_run_of("jobA", "sa1", 12))
    assert first.error is None
    # The NEXT run of the same job (a fresh session), a quote this time.
    second = await _via(t).twitter_quote(TwitterQuoteAction(tweet_id="1", text="rung 2"),
                                         execution_context=_run_of("jobA", "sa2", 12))
    assert second.error and "post_spacing_hours" in second.error
    assert "POST NOTHING" in second.error
    assert t.client.create_tweet.call_count == 1


@pytest.mark.asyncio
async def test_another_jobs_post_does_not_count(monkeypatch, no_cooldown):
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = [_resp("1"), _resp("2")]
    other = await _via(t).twitter_post(TwitterPostAction(text="announcer"),
                                       execution_context=_run_of("jobB", "sb1"))
    assert other.error is None
    mine = await _via(t).twitter_post(TwitterPostAction(text="rung 1"),
                                      execution_context=_run_of("jobA", "sa3", 12))
    assert mine.error is None


@pytest.mark.asyncio
async def test_a_job_without_spacing_is_never_gated(monkeypatch, no_cooldown):
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = [_resp("1"), _resp("2")]
    for sid in ("sc1", "sc2"):
        res = await _via(t).twitter_post(TwitterPostAction(text=f"post {sid}"),
                                         execution_context=_run_of("jobC", sid))
        assert res.error is None


def test_clearing_the_spacing_forgets_it():
    autonomy_marker.note_job("jobD", "t", post_spacing_hours=12)
    assert autonomy_marker.post_spacing_for_job("jobD") == 12.0
    autonomy_marker.note_job("jobD", "t", post_spacing_hours=None)
    assert autonomy_marker.post_spacing_for_job("jobD") is None


def test_the_spaced_set_is_derived_from_the_policy_rows():
    """W4: the write rows minus the reply, plus the repost — not a hand list."""
    from polyrob_x.post_spacing import spaced_actions
    assert spaced_actions() == {"twitter_post", "twitter_thread", "twitter_quote",
                                "twitter_retweet"}
