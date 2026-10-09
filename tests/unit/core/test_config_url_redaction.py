"""Config output must not disclose credentials hidden in endpoint URLs."""
import pytest
from click.testing import CliRunner

from core.security.redaction import redact_config_urls


@pytest.mark.parametrize("url", [
    "https://user:private-value@example.test/path-secret",
    "wss://example.test/socket-secret?token=query-secret#fragment-secret",
    "postgresql://user:private-value@example.test/database-secret",
    "https://example.test:bad/query-secret",
])
def test_config_endpoint_redaction(url):
    result = redact_config_urls(url)
    for secret in ("private-value", "path-secret", "socket-secret", "query-secret",
                   "fragment-secret", "database-secret"):
        assert secret not in result


def test_embedded_endpoints_are_redacted_and_types_preserved():
    result = redact_config_urls('https://a.test/key-one,wss://b.test/key-two')
    assert result == 'https://a.test/…,wss://b.test/…'
    assert redact_config_urls(42) == 42
    assert redact_config_urls(False) is False
    assert redact_config_urls("ordinary-model-name") == "ordinary-model-name"


def test_all_config_displays_hide_endpoint_credentials(tmp_path, monkeypatch):
    from cli.commands.config import config
    from core import config_service
    from core.flags import resolve_flag
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path))
    value = "https://user:private-value@example.test/path-secret?token=query-secret"
    (tmp_path / ".env").write_text(f"WEBVIEW_PUBLIC_URL={value}\nCUSTOM_ENDPOINT={value}\n")
    monkeypatch.setenv("WEBVIEW_PUBLIC_URL", value)
    monkeypatch.chdir(tmp_path)
    displays = [
        resolve_flag("WEBVIEW_PUBLIC_URL", {"WEBVIEW_PUBLIC_URL": value}),
        resolve_flag("WEBVIEW_PUBLIC_URL", {}, lambda _: (value, "dynamic")),
        config_service.explain("WEBVIEW_PUBLIC_URL"),
        config_service.set_value("WEBVIEW_PUBLIC_URL", value, scope="global"),
        config_service.set_value("CUSTOM_ENDPOINT", value, scope="global", allow_unknown=True),
        config_service.set_value("GOAL_DAILY_QUOTA", value, scope="global"),
        config_service.set_value("AUTONOMY_MODE", value, scope="global"),
    ]
    result = CliRunner().invoke(config, ["show", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    displays.append(result.output)
    for display in displays:
        text = str(display)
        for secret in ("private-value", "path-secret", "query-secret"):
            assert secret not in text


def test_secret_dynamic_defaults_are_masked():
    from core.flags import resolve_flag
    result = resolve_flag("BROWSER_WSS_URL", {}, lambda _: ("private-value", "dynamic"))
    assert "private-value" not in repr(result)
