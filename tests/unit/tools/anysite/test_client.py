import pytest
from tools.anysite.client import build_api_argv, binary_available


def test_build_api_argv_basic():
    argv = build_api_argv("/api/linkedin/user", {"user": "satyanadella"}, "json")
    assert argv[0] == "anysite"
    assert argv[1] == "--non-interactive"
    assert argv[2] == "api"
    assert argv[3] == "/api/linkedin/user"
    assert "user=satyanadella" in argv
    assert "--format" in argv and "json" in argv


def test_build_api_argv_no_params():
    argv = build_api_argv("/api/yc/companies", None, "json")
    assert argv[:4] == ["anysite", "--non-interactive", "api", "/api/yc/companies"]
    assert "--format" in argv


def test_build_api_argv_rejects_unsafe_param_values():
    # params are passed as key=value tokens (no shell); a value with a newline is rejected
    with pytest.raises(ValueError):
        build_api_argv("/api/x", {"q": "a\nb"}, "json")


def test_binary_available_is_bool():
    assert isinstance(binary_available(), bool)
