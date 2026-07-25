"""T2.4 Task 1 — MCPServerConfig.auth block: schema + ${VAR} resolution.

NOTHING reads this field yet (unwired) — these tests only cover the schema and
the resolver's fault-isolation branch, mirroring
tests/unit/tools/mcp/test_config_env_fault_isolation.py.
"""
import pytest

from tools.mcp.config import (
    MCPConfig, MCPServerConfig, MCPServerType,
    resolve_config_environment_variables,
)


def _http_server(url="https://server.example", auth=None):
    return MCPServerConfig(type=MCPServerType.HTTP, url=url, auth=auth)


# --- schema: field exists, defaults, verbatim passthrough ----------------------

def test_auth_defaults_to_none():
    cfg = _http_server()
    assert cfg.auth is None


def test_auth_block_accepted_and_preserved_verbatim():
    auth = {
        "provider": "generic_oauth2",
        "client_id": "my-client-id",
        "auth_url": "https://idp.example/authorize",
        "token_url": "https://idp.example/token",
        "scopes": ["read", "write"],
    }
    cfg = _http_server(auth=auth)
    assert cfg.auth == auth


# --- ${VAR} resolution inside auth ----------------------------------------------

def test_var_in_auth_client_secret_resolves(monkeypatch):
    monkeypatch.setenv("ROB_TEST_OAUTH_SECRET", "s3cr3t-value")
    cfg = MCPConfig(
        enabled=True,
        servers={
            "svc": _http_server(auth={
                "provider": "generic_oauth2",
                "client_id": "cid",
                "client_secret": "${ROB_TEST_OAUTH_SECRET}",
                "auth_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
            }),
        },
    )
    resolved = resolve_config_environment_variables(cfg)
    assert resolved.servers["svc"].auth["client_secret"] == "s3cr3t-value"
    # non-var fields pass through untouched
    assert resolved.servers["svc"].auth["client_id"] == "cid"


def test_var_in_auth_scopes_list_resolves(monkeypatch):
    monkeypatch.setenv("ROB_TEST_OAUTH_SCOPE", "custom.scope")
    cfg = MCPConfig(
        enabled=True,
        servers={
            "svc": _http_server(auth={
                "client_id": "cid",
                "auth_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
                "scopes": ["read", "${ROB_TEST_OAUTH_SCOPE}"],
            }),
        },
    )
    resolved = resolve_config_environment_variables(cfg)
    assert resolved.servers["svc"].auth["scopes"] == ["read", "custom.scope"]


def test_no_auth_block_resolves_cleanly():
    cfg = MCPConfig(enabled=True, servers={"svc": _http_server()})
    resolved = resolve_config_environment_variables(cfg)
    assert resolved.servers["svc"].auth is None


# --- fault isolation: a missing ${VAR} in auth drops ONLY that server ----------

def test_missing_auth_var_drops_only_affected_server(monkeypatch):
    monkeypatch.delenv("ROB_TEST_MISSING_OAUTH_SECRET", raising=False)
    cfg = MCPConfig(
        enabled=True,
        servers={
            "good": _http_server(url="https://good.example"),
            "bad": _http_server(url="https://bad.example", auth={
                "client_id": "cid",
                "client_secret": "${ROB_TEST_MISSING_OAUTH_SECRET}",
                "auth_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
            }),
        },
    )
    resolved = resolve_config_environment_variables(cfg)
    assert "good" in resolved.servers
    assert "bad" not in resolved.servers


def test_missing_auth_var_in_one_server_does_not_affect_other_auth_server(monkeypatch):
    monkeypatch.delenv("ROB_TEST_MISSING_OAUTH_SECRET2", raising=False)
    monkeypatch.setenv("ROB_TEST_PRESENT_OAUTH_SECRET", "present-value")
    cfg = MCPConfig(
        enabled=True,
        servers={
            "good_auth": _http_server(url="https://good.example", auth={
                "client_id": "cid",
                "client_secret": "${ROB_TEST_PRESENT_OAUTH_SECRET}",
                "auth_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
            }),
            "bad_auth": _http_server(url="https://bad.example", auth={
                "client_id": "cid",
                "client_secret": "${ROB_TEST_MISSING_OAUTH_SECRET2}",
                "auth_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
            }),
        },
    )
    resolved = resolve_config_environment_variables(cfg)
    assert "good_auth" in resolved.servers
    assert resolved.servers["good_auth"].auth["client_secret"] == "present-value"
    assert "bad_auth" not in resolved.servers
