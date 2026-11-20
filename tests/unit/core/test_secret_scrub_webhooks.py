"""L-21 (audit 2026-11-21): a Discord or Slack webhook URL IS its credential (anyone
holding it can post as the hook); the shape battery redacts it with no key name
in front of it, and leaves the non-secret part readable."""
import pytest

from core.secret_scrub import scrub_secret_shapes

_DISCORD_TOKEN = "x" * 68  # a fixture, not a credential: low entropy on purpose
_SLACK_TAIL = "T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"


@pytest.mark.parametrize("url", [
    f"https://discord.com/api/webhooks/123456789012345678/{_DISCORD_TOKEN}",
    f"https://discordapp.com/api/webhooks/123456789012345678/{_DISCORD_TOKEN}",
    f"https://ptb.discord.com/api/v10/webhooks/123456789012345678/{_DISCORD_TOKEN}",
])
def test_a_discord_webhook_url_is_redacted(url):
    out = scrub_secret_shapes(f"ALERT_HOOK {url} (set)")
    assert _DISCORD_TOKEN not in out
    assert "123456789012345678" not in out
    assert "/webhooks/" in out and "(set)" in out


@pytest.mark.parametrize("kind", ["services", "workflows", "triggers"])
def test_a_slack_hook_url_is_redacted(kind):
    out = scrub_secret_shapes(f"notify https://hooks.slack.com/{kind}/{_SLACK_TAIL} done")
    assert "XXXXXXXXXXXXXXXXXXXXXXXX" not in out and "T00000000" not in out
    assert f"hooks.slack.com/{kind}/" in out and out.endswith(" done")


def test_a_plain_discord_or_slack_link_is_kept():
    text = "see https://discord.com/channels/1/2 and https://slack.com/help"
    assert scrub_secret_shapes(text) == text
