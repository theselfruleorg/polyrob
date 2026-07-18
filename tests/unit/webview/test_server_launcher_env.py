"""Regression (P0): webview/server_launcher.load_environment() must NEVER print
secret values. The hand-rolled parser did print(f"  Set {key}={value}") for every
line, leaking secrets from /etc/webview.env into journalctl on every restart.
"""
import os

import pytest


def test_load_environment_never_prints_values(tmp_path, capsys):
    envf = tmp_path / "webview.env"
    envf.write_text(
        "WEBVIEW_TEST_SECRET_A=supersecretvalue_aaa\n"
        "WEBVIEW_TEST_SECRET_B=anothersecret_bbb\n"
        "# a comment\n"
    )
    from webview.server_launcher import load_environment
    try:
        load_environment(str(envf))
        out = capsys.readouterr().out
        # Values must not appear anywhere in stdout.
        assert "supersecretvalue_aaa" not in out
        assert "anothersecret_bbb" not in out
        # But the vars were actually loaded.
        assert os.environ.get("WEBVIEW_TEST_SECRET_A") == "supersecretvalue_aaa"
        assert os.environ.get("WEBVIEW_TEST_SECRET_B") == "anothersecret_bbb"
        # Key names in the log line are fine (not secret).
        assert "WEBVIEW_TEST_SECRET_A" in out
    finally:
        os.environ.pop("WEBVIEW_TEST_SECRET_A", None)
        os.environ.pop("WEBVIEW_TEST_SECRET_B", None)


def test_load_environment_missing_file_is_noop(tmp_path, capsys):
    from webview.server_launcher import load_environment
    load_environment(str(tmp_path / "nope.env"))
    assert capsys.readouterr().out == ""
