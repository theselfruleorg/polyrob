"""The ``twitter`` cron delivery channel (067 P3b): a cron job with
``deliver=twitter`` publishes its report as a public post.

Registered through the ``cron.delivery_channel`` hook (``polyrob_x.pack``); it
moved here from ``cron/delivery.py`` with the tool. The shared pieces — the
autonomous delivery context and the effect record — stay in ``cron.delivery``.
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _build_twitter_tool(config: Any, container: Any) -> Any:
    """Construction seam (033 T0.2) so a test can assert the ACTION is used."""
    from polyrob_x.twitter_tool import TwitterTool
    return TwitterTool("twitter", config, container)


async def deliver_post(task_agent: Any, job: Any, final: str) -> bool:
    from cron.delivery import _config_and_container, _delivery_context, _record_delivery_write
    from polyrob_x.twitter_tool import TwitterPostAction
    config, container = _config_and_container(task_agent)
    tool = _build_twitter_tool(config, container)
    text = final.strip()
    if len(text) > 280:
        text = text[:277] + "..."
    # 033 T0.2: route through the ACTION, never TwitterTool.post(). The raw
    # helper skips _check_ready (so TWITTER_ENABLED=false did not stop it), the
    # hourly rate limit, TWITTER_REQUIRE_APPROVAL, the cross-session repeat-post
    # cooldown, the 031 pause gate and the social_write record — a cron job with
    # deliver=twitter published with zero governance.
    res = await tool.twitter_post(TwitterPostAction(text=text),
                                  execution_context=_delivery_context(job))
    _record_delivery_write(job, "twitter", "twitter_post", res,
                           target="open", fingerprint=text)
    if getattr(res, "error", None):
        logger.warning("cron delivery: twitter_post refused for job %s: %s",
                       getattr(job, "id", "?"), res.error)
        return False
    return True
