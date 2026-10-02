"""The startup self-test must ask `get_me` with a credential it is allowed to use.

⚠️ The defect, printed on every service start since the OAuth2 DM rail landed:

    ERROR RateLimitManager: Error in rate limit context for twitter_users: 403 Forbidden
    Authenticating with OAuth 2.0 Application-Only is forbidden for this endpoint.
    Supported authentication types are [OAuth 1.0a User Context, OAuth 2.0 User Context].
    WARNING TwitterTool.twitter: Twitter API test failed: ...

Nothing was wrong with the account: posting and `twitter_get_mentions` both worked
on the same service start. The probe simply used **app-only** auth on a
**user-context-only** endpoint, because `_test_api_connection` chose
``user_auth=False`` whenever an OAuth2 user token EXISTS — while the client's
bearer is ``self.bearer_token or self.oauth2_access_token``, so with an app-only
bearer configured the client was carrying the app-only token, not the user one.

``user_auth=False`` means "use this client's bearer". It is only correct when
that bearer IS the user token. Otherwise OAuth 1.0a user context is the one that
works, and when NEITHER user-context credential exists there is no legal way to
call ``get_me`` at all — so the probe is skipped and said to be skipped, rather
than issued in a mode that is known to be forbidden.

The cost of getting this wrong was not the failed probe (init tolerates it) but
the false ERROR: the X rail looked broken on every start while it was healthy,
and that line had to be filtered out of health greps by hand for weeks.
"""
import pytest

pytest.importorskip("polyrob_x")

import logging

import pytest

from polyrob_x.twitter_tool import TwitterTool


def _tool(*, bearer, oauth2, oauth1):
    """A TwitterTool carrying only the credential fields the probe reads."""
    tool = TwitterTool.__new__(TwitterTool)
    tool.bearer_token = bearer
    tool.oauth2_access_token = oauth2
    tool._oauth1_available = oauth1
    return tool


# --- which mode the probe picks ------------------------------------------------ #

def test_an_app_only_bearer_never_probes_in_app_only_mode():
    """The production case: both credentials configured. The client's bearer is
    the APP-ONLY token, so the probe must use OAuth 1.0a user context."""
    assert _tool(bearer="APP-ONLY", oauth2="USER-TOKEN", oauth1=True)._probe_user_auth() is True


def test_a_user_token_bearer_probes_with_that_bearer():
    """No app-only token, so the client's bearer IS the OAuth2 user token —
    which `get_me` accepts, and `user_auth=False` is what selects it."""
    assert _tool(bearer=None, oauth2="USER-TOKEN", oauth1=False)._probe_user_auth() is False


def test_oauth1_only_probes_with_user_context():
    assert _tool(bearer="APP-ONLY", oauth2=None, oauth1=True)._probe_user_auth() is True


def test_a_user_token_bearer_is_preferred_over_oauth1():
    """Both would work; the bearer the client already holds costs no signing."""
    assert _tool(bearer=None, oauth2="USER-TOKEN", oauth1=True)._probe_user_auth() is False


def test_no_user_context_credential_yields_no_probe_mode():
    """App-only bearer and nothing else: `get_me` is unreachable. None means
    "do not ask" — NOT "ask in app-only mode and log the refusal"."""
    assert _tool(bearer="APP-ONLY", oauth2=None, oauth1=False)._probe_user_auth() is None


def test_an_empty_string_credential_is_absent():
    assert _tool(bearer="", oauth2="USER-TOKEN", oauth1=False)._probe_user_auth() is False
    assert _tool(bearer="APP-ONLY", oauth2="", oauth1=False)._probe_user_auth() is None


# --- what the probe actually does ---------------------------------------------- #

@pytest.mark.asyncio
async def test_the_probe_passes_the_mode_it_chose():
    calls = {}

    tool = _tool(bearer="APP-ONLY", oauth2="USER-TOKEN", oauth1=True)
    tool.logger = logging.getLogger("test.twitter")
    tool.client = object()
    tool._initializing = True

    async def _make_request(func, endpoint_type, **kwargs):
        calls.update(endpoint_type=endpoint_type, **kwargs)
        return type("R", (), {"data": {"username": "tmachinrobot"}})()

    tool._make_request = _make_request
    tool.client = type("C", (), {"get_me": lambda *a, **k: None})()
    await TwitterTool._test_api_connection(tool)

    assert calls["user_auth"] is True          # NOT the forbidden app-only mode
    assert calls["endpoint_type"] == "users"


@pytest.mark.asyncio
async def test_no_usable_credential_skips_the_call_entirely(caplog):
    tool = _tool(bearer="APP-ONLY", oauth2=None, oauth1=False)
    tool.logger = logging.getLogger("test.twitter.skip")
    tool.client = object()
    tool._initializing = True

    async def _must_not_run(*a, **k):
        raise AssertionError("probed an endpoint no configured credential may call")

    tool._make_request = _must_not_run
    with caplog.at_level(logging.INFO, logger="test.twitter.skip"):
        await TwitterTool._test_api_connection(tool)

    # Skipped, and SAID to be skipped — silence would read as "the probe passed".
    assert any("user-context" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_failing_probe_still_does_not_break_initialization():
    """Unchanged behaviour: init tolerates a probe failure. Only the auth mode
    and the skip path are new."""
    tool = _tool(bearer=None, oauth2="USER-TOKEN", oauth1=False)
    tool.logger = logging.getLogger("test.twitter.fail")
    tool.client = object()
    tool._initializing = True

    async def _boom(*a, **k):
        raise RuntimeError("upstream down")

    tool._make_request = _boom
    await TwitterTool._test_api_connection(tool)   # must not raise
