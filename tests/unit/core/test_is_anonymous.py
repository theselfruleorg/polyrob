"""SSOT identity predicate (user-identity-tenant finalization, P0).

`is_anonymous` is the single source of truth for "is this the anonymous/default
bucket?" — every tenant guard reads it instead of its own empty-only check, closing
the sentinel-bypass class (findings F1/F4). `"local"` is a real named CLI tenant and
must NEVER be treated as anonymous.
"""
from core.identity import ANON_USER_ID, is_anonymous, normalize_user_id


def test_anon_true_for_absent_and_sentinels():
    for value in (
        None,
        "",
        "   ",
        "_anonymous_",
        "system",
        "x402_user",
        "authenticated_api_user",
        "api_user",
    ):
        assert is_anonymous(value) is True, value


def test_anon_false_for_real_named_tenants():
    for value in ("local", "alice", "u_0123456789abcdef01234567", "0xabcDEF"):
        assert is_anonymous(value) is False, value


def test_local_is_a_real_tenant_not_anonymous():
    # Load-bearing: the CLI runs as "local"; if it were anonymous, default-true
    # MEMORY_REQUIRE_USER_ID would block all CLI memory recall.
    assert is_anonymous("local") is False


def test_normalize_strips_and_collapses_blank():
    assert normalize_user_id("  alice  ") == "alice"
    assert normalize_user_id(None) == ""
    assert normalize_user_id("   ") == ""


def test_canonical_token_matches_default_user_id():
    from agents.task.constants import DEFAULT_USER_ID

    assert ANON_USER_ID == DEFAULT_USER_ID == "_anonymous_"
