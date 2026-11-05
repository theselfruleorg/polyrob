import pytest


@pytest.fixture(autouse=True)
def _installer_sends_no_count(monkeypatch):
    """A test run of install.sh must not reach the live install count."""
    monkeypatch.setenv("DO_NOT_TRACK", "1")
