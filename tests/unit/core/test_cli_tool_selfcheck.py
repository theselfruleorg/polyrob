"""Startup capability self-check (structural review #1) — surfaces tool-registration drift.

Guards the log-only self-check that reports which registerable tools resolved to a container
service. This drift (allowlisted but not service-registered) caused the twitter + web_fetch
outages on 2026-07-01.
"""
import inspect
import core.bootstrap as bootstrap


def test_register_cli_tools_has_capability_self_check():
    src = inspect.getsource(bootstrap.register_cli_tools)
    assert "CLI tool self-check" in src
    assert "container.has_service(t)" in src
