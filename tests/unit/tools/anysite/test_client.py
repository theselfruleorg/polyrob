import pytest
from tools.anysite.client import build_api_argv, build_describe_argv, binary_available


def test_build_api_argv_basic():
    argv = build_api_argv("/api/linkedin/user", {"user": "satyanadella"}, "json")
    # argv[0] is now the RESOLVED path (the venv bin is not on a
    # systemd unit's PATH), so assert the program, not the literal.
    assert argv[0].endswith("anysite")
    assert argv[1] == "--non-interactive"
    assert argv[2] == "api"
    assert argv[3] == "/api/linkedin/user"
    assert "user=satyanadella" in argv
    assert "--format" in argv and "json" in argv


def test_build_api_argv_no_params():
    argv = build_api_argv("/api/yc/companies", None, "json")
    assert argv[0].endswith("anysite")   # RESOLVED path, not a bare PATH lookup
    assert argv[1:4] == ["--non-interactive", "api", "/api/yc/companies"]
    assert "--format" in argv


def test_build_api_argv_rejects_unsafe_param_values():
    # params are passed as key=value tokens (no shell); a value with a newline is rejected
    with pytest.raises(ValueError):
        build_api_argv("/api/x", {"q": "a\nb"}, "json")


def test_binary_available_is_bool():
    assert isinstance(binary_available(), bool)


# ── build_describe_argv (2026-09-15, owner rail "fix Anysite tool") ──────────
# The CLI has ALWAYS had `anysite describe` (endpoint discovery: search by
# keyword, or full input/output schema for one endpoint) but the tool never
# exposed it — so agents guessed endpoint paths and burned steps on Not Found
# and param-validation churn (observed 09-12 17:23, 09-15 19:37).

def test_build_describe_argv_search():
    argv = build_describe_argv(search="twitter")
    assert argv[1:4] == ["--non-interactive", "describe", "--search"]
    assert argv[4] == "twitter"
    # paths-only + JSON: a search can match dozens of endpoints; the quiet
    # form keeps the agent's context small
    assert "--quiet" in argv and "--json" in argv


def test_build_describe_argv_endpoint():
    argv = build_describe_argv(endpoint="/api/twitter/search/posts")
    assert argv[1:4] == ["--non-interactive", "describe", "/api/twitter/search/posts"]
    assert "--json" in argv
    # one endpoint gets the FULL schema (input params + output fields) — no quiet
    assert "--quiet" not in argv


def test_build_describe_argv_rejects_unsafe_tokens():
    with pytest.raises(ValueError):
        build_describe_argv(endpoint="/api/x\ninjected")
    with pytest.raises(ValueError):
        build_describe_argv(search="a\rb")
