"""045 Phase 1: the security event log is default-ON and falsey-disable."""
import pytest

from core.security_flags import security_event_log_enabled


def test_default_is_on(monkeypatch):
    monkeypatch.delenv("SECURITY_EVENT_LOG_ENABLED", raising=False)
    assert security_event_log_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "off", "no", "", "OFF", "False"])
def test_falsey_values_disable(monkeypatch, val):
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", val)
    assert security_event_log_enabled() is False


def test_truthy_value_enables(monkeypatch):
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "true")
    assert security_event_log_enabled() is True
