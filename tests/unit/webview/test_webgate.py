"""Unit tests for webview.webgate — the single-user vs multitenant config object."""
import importlib

import pytest


@pytest.fixture
def webgate(monkeypatch):
    """Fresh webgate module with a clean env for each test."""
    # Clear any webgate/webview env that could leak between tests.
    for k in (
        "WEBGATE_MULTITENANT", "WEBGATE_HOST", "WEBGATE_PORT",
        "WEBVIEW_HOST", "WEBVIEW_PORT", "POLYROB_LOCAL_OWNER",
        "POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
        "SURFACE_SUPER_ADMIN_USER_IDS", "POLYROB_INSTANCE_ID", "BOT_INSTANCE_ID",
        "POLYROB_POSTURE", "POLYROB_CONSOLE_NAME", "POLYROB_SUPPORT_URL",
        "POLYROB_SUPPORT_HANDLE", "POLYROB_BRAND_URL",
        "POLYROB_ORG_URL", "POLYROB_TERMS_URL", "POLYROB_PRIVACY_URL", "WEBVIEW_DOMAIN",
    ):
        monkeypatch.delenv(k, raising=False)
    import webview.webgate as wg
    return importlib.reload(wg)


def test_is_multitenant_default_off(webgate):
    assert webgate.is_multitenant() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " On "])
def test_is_multitenant_truthy(webgate, monkeypatch, val):
    monkeypatch.setenv("WEBGATE_MULTITENANT", val)
    assert webgate.is_multitenant() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_is_multitenant_falsey(webgate, monkeypatch, val):
    monkeypatch.setenv("WEBGATE_MULTITENANT", val)
    assert webgate.is_multitenant() is False


def test_bind_host_loopback_when_single_user(webgate):
    assert webgate.bind_host() == "127.0.0.1"


def test_bind_host_all_interfaces_when_multitenant(webgate, monkeypatch):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true")
    assert webgate.bind_host() == "0.0.0.0"


def test_bind_host_env_override_wins(webgate, monkeypatch):
    monkeypatch.setenv("WEBGATE_HOST", "10.0.0.5")
    assert webgate.bind_host() == "10.0.0.5"
    # Even in multitenant mode, the explicit override still wins.
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true")
    assert webgate.bind_host() == "10.0.0.5"


def test_bind_host_webview_host_override(webgate, monkeypatch):
    monkeypatch.setenv("WEBVIEW_HOST", "192.168.1.2")
    assert webgate.bind_host() == "192.168.1.2"


def test_bind_port_default_5050(webgate):
    assert webgate.bind_port() == 5050


def test_bind_port_env_override(webgate, monkeypatch):
    monkeypatch.setenv("WEBGATE_PORT", "8123")
    assert webgate.bind_port() == 8123
    monkeypatch.setenv("WEBVIEW_PORT", "9001")
    # WEBGATE_PORT takes precedence over WEBVIEW_PORT.
    assert webgate.bind_port() == 8123


def test_bind_port_webview_port_fallback(webgate, monkeypatch):
    monkeypatch.setenv("WEBVIEW_PORT", "7000")
    assert webgate.bind_port() == 7000


def test_local_owner_id_falls_back_to_rob(webgate):
    assert webgate.local_owner_id() == "rob"


def test_local_owner_id_env_override(webgate, monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL_OWNER", "alice")
    assert webgate.local_owner_id() == "alice"


def test_local_owner_id_owner_principal_wins(webgate, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-123")
    monkeypatch.setenv("POLYROB_LOCAL_OWNER", "alice")
    assert webgate.local_owner_id() == "owner-123"


def test_posture_default_is_local(webgate):
    assert webgate.posture() == "local"


def test_posture_explicit_env_wins(webgate, monkeypatch):
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    assert webgate.posture() == "own_ops"


@pytest.mark.parametrize("val,expected", [
    ("local", "local"), ("LOCAL", "local"),
    ("own_ops", "own_ops"), ("Own_Ops", "own_ops"),
    ("multitenant", "multitenant"), ("MULTITENANT", "multitenant"),
])
def test_posture_explicit_env_case_insensitive(webgate, monkeypatch, val, expected):
    monkeypatch.setenv("POLYROB_POSTURE", val)
    assert webgate.posture() == expected


def test_posture_explicit_env_wins_over_webgate_multitenant(webgate, monkeypatch):
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true")
    assert webgate.posture() == "local"


def test_posture_backcompat_webgate_multitenant_true(webgate, monkeypatch):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true")
    assert webgate.posture() == "multitenant"


def test_posture_derives_own_ops_from_nonloopback_host_override(webgate, monkeypatch):
    monkeypatch.setenv("WEBGATE_HOST", "0.0.0.0")
    assert webgate.posture() == "own_ops"


def test_posture_derives_local_from_loopback_host_override(webgate, monkeypatch):
    monkeypatch.setenv("WEBGATE_HOST", "127.0.0.1")
    assert webgate.posture() == "local"


def test_posture_derives_own_ops_from_webview_host_override(webgate, monkeypatch):
    monkeypatch.setenv("WEBVIEW_HOST", "10.0.0.5")
    assert webgate.posture() == "own_ops"


def test_is_own_ops_true_only_for_own_ops(webgate, monkeypatch):
    assert webgate.is_own_ops() is False
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    assert webgate.is_own_ops() is True


def test_is_local_true_only_for_local(webgate, monkeypatch):
    assert webgate.is_local() is True
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")
    assert webgate.is_local() is False


def test_requires_owner_login_false_for_local(webgate):
    assert webgate.requires_owner_login() is False


@pytest.mark.parametrize("val", ["own_ops", "multitenant"])
def test_requires_owner_login_true_for_public_postures(webgate, monkeypatch, val):
    monkeypatch.setenv("POLYROB_POSTURE", val)
    assert webgate.requires_owner_login() is True


def test_is_multitenant_still_backed_by_posture(webgate, monkeypatch):
    # Back-compat accessor stays byte-identical in observable behavior.
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")
    assert webgate.is_multitenant() is True
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    assert webgate.is_multitenant() is False


def test_console_display_name_default(webgate):
    assert webgate.console_display_name() == "POLYROB Console"


def test_console_display_name_env_override(webgate, monkeypatch):
    monkeypatch.setenv("POLYROB_CONSOLE_NAME", "Rob Console")
    assert webgate.console_display_name() == "Rob Console"


def test_branding_config_defaults(webgate):
    b = webgate.branding_config()
    assert b["support_url"] == "https://t.me/tmachinrobot"
    assert b["support_display"] == "t.me/tmachinrobot"
    assert b["support_handle"] == "@TMACHINROBOT"
    assert b["brand_url"] == "https://your-polyrob-host.example"
    assert b["brand_display"] == "your-polyrob-host.example"
    assert b["org_url"] == "https://theselfrule.org"
    assert b["org_display"] == "theselfrule.org"


def test_branding_config_env_overrides(webgate, monkeypatch):
    monkeypatch.setenv("POLYROB_SUPPORT_URL", "https://t.me/myinstance")
    monkeypatch.setenv("POLYROB_SUPPORT_HANDLE", "@MYINSTANCE")
    monkeypatch.setenv("POLYROB_BRAND_URL", "https://example.com")
    monkeypatch.setenv("POLYROB_ORG_URL", "https://org.example.com")
    b = webgate.branding_config()
    assert b["support_url"] == "https://t.me/myinstance"
    assert b["support_display"] == "t.me/myinstance"
    assert b["support_handle"] == "@MYINSTANCE"
    assert b["brand_url"] == "https://example.com"
    assert b["brand_display"] == "example.com"
    assert b["org_url"] == "https://org.example.com"
    assert b["org_display"] == "org.example.com"


def test_branding_config_legal_links_default(webgate):
    b = webgate.branding_config()
    assert b["terms_url"] == "https://your-polyrob-host.example/terms"
    assert b["privacy_url"] == "https://your-polyrob-host.example/privacy"


def test_branding_config_legal_links_follow_brand_url_override(webgate, monkeypatch):
    monkeypatch.setenv("POLYROB_BRAND_URL", "https://example.com")
    b = webgate.branding_config()
    assert b["terms_url"] == "https://example.com/terms"
    assert b["privacy_url"] == "https://example.com/privacy"


def test_branding_config_legal_links_explicit_override(webgate, monkeypatch):
    monkeypatch.setenv("POLYROB_TERMS_URL", "https://legal.example.com/tos")
    monkeypatch.setenv("POLYROB_PRIVACY_URL", "https://legal.example.com/privacy")
    b = webgate.branding_config()
    assert b["terms_url"] == "https://legal.example.com/tos"
    assert b["privacy_url"] == "https://legal.example.com/privacy"


# ── H2a (B1-LOW): invalid POLYROB_POSTURE warns instead of silently falling through ──

def test_invalid_posture_logs_warning_and_still_derives(webgate, monkeypatch, caplog):
    monkeypatch.setenv("POLYROB_POSTURE", "own-ops")  # typo: dash instead of underscore
    with caplog.at_level("WARNING", logger="webview.webgate"):
        result = webgate.posture()
    # No host override, WEBGATE_MULTITENANT unset -> derivation falls to "local".
    assert result == "local"
    assert any("own-ops" in rec.message for rec in caplog.records), caplog.text


def test_invalid_posture_warning_then_derives_multitenant(webgate, monkeypatch, caplog):
    monkeypatch.setenv("POLYROB_POSTURE", "bogus")
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true")
    with caplog.at_level("WARNING", logger="webview.webgate"):
        result = webgate.posture()
    assert result == "multitenant"
    assert any("bogus" in rec.message for rec in caplog.records), caplog.text


def test_valid_posture_does_not_warn(webgate, monkeypatch, caplog):
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    with caplog.at_level("WARNING", logger="webview.webgate"):
        result = webgate.posture()
    assert result == "own_ops"
    assert caplog.records == []


def test_empty_posture_does_not_warn(webgate, caplog):
    # Unset (the default) is not "invalid" -- must not warn.
    with caplog.at_level("WARNING", logger="webview.webgate"):
        result = webgate.posture()
    assert result == "local"
    assert caplog.records == []
