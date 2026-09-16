"""The launch quote SHOWS the metadata it is about to commit (042c).

These fields are constructor arguments with no setter: a logo URL with a typo
in it is permanent. So the report has to echo the strings rather than merely
accept them, and it has to say so when the logo is missing — at the shared
header, where the CLI and the agent's own action see it too, not at one seat.
"""
from tools.launchpad.tool import _metadata_block


class _P:
    def __init__(self, logo="", description="", twitter="", website=""):
        self.logo, self.description = logo, description
        self.twitter, self.website = twitter, website


def test_every_supplied_field_is_echoed():
    out = _metadata_block(_P(logo="https://i/r.png", description="a coin",
                             twitter="https://x.com/r", website="https://r.dev"))
    for value in ("https://i/r.png", "a coin", "https://x.com/r", "https://r.dev"):
        assert value in out


def test_a_missing_logo_is_called_out():
    assert "NO LOGO" in _metadata_block(_P(description="a coin"))


def test_a_present_logo_draws_no_warning():
    assert "NO LOGO" not in _metadata_block(_P(logo="ipfs://x"))


def test_the_unset_fields_are_named_as_permanent():
    out = _metadata_block(_P(logo="ipfs://x"))
    assert "description" in out and "permanent once launched" in out


def test_a_fully_populated_launch_has_nothing_left_to_warn_about():
    out = _metadata_block(_P(logo="ipfs://x", description="d",
                             twitter="t", website="w"))
    assert "NO LOGO" not in out and "not set" not in out
