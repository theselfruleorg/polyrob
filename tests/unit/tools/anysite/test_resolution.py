"""Finding the CLI and its credential (prod, 2026-09-12).

AnySite had been dark on prod for months and the tool said the wrong thing about
why. Two independent defects, each of which alone is enough to break it:

  * `shutil.which("anysite")` returned None on a box where the binary WAS
    installed. anysite-cli is a declared dependency, so pip puts its console
    script beside the running interpreter (`/opt/polyrob/venv/bin/anysite`); a
    systemd unit execs that python directly and inherits the SYSTEM PATH, which
    does not contain that directory.
  * the credential was absent, and the message blamed the package — so the
    operator checked the package, found it present, and concluded the capability
    was broken rather than unconfigured.
"""
import os
import sys

import pytest

from tools.anysite import client
from tools.anysite.tool import _unavailable_reason


def test_the_binary_is_found_beside_the_interpreter_not_only_on_PATH(tmp_path, monkeypatch):
    """The exact prod shape: installed in the venv, absent from PATH."""
    fake = tmp_path / "anysite"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(client.shutil, "which", lambda _n: None)
    assert client.binary_path() == str(fake)
    assert client.binary_available() is True


def test_it_still_falls_back_to_PATH(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nothing-here" / "python"))
    monkeypatch.setattr(client.shutil, "which", lambda _n: "/usr/bin/anysite")
    assert client.binary_path() == "/usr/bin/anysite"


def test_a_non_executable_file_is_not_the_binary(tmp_path, monkeypatch):
    dud = tmp_path / "anysite"
    dud.write_text("not executable")
    dud.chmod(0o644)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(client.shutil, "which", lambda _n: None)
    assert client.binary_path() is None


def test_argv_carries_the_RESOLVED_path(tmp_path, monkeypatch):
    """Exec-ing the bare name is what failed under systemd."""
    fake = tmp_path / "anysite"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(client.shutil, "which", lambda _n: None)
    assert client.build_api_argv("/api/x")[0] == str(fake)


# --- the credential ---------------------------------------------------------

def test_either_env_name_carries_the_key(monkeypatch):
    """AnySite's own template calls it ANYSITE_ACCESS_TOKEN (their REST header is
    literally `access-token`); this codebase read only ANYSITE_API_KEY, so an
    operator who copied the vendor template went silently dark."""
    monkeypatch.delenv("ANYSITE_API_KEY", raising=False)
    monkeypatch.delenv("ANYSITE_ACCESS_TOKEN", raising=False)
    assert client.api_key() is None

    monkeypatch.setenv("ANYSITE_ACCESS_TOKEN", "tok-from-vendor-template")
    assert client.api_key() == "tok-from-vendor-template"

    monkeypatch.setenv("ANYSITE_API_KEY", "tok-native")
    assert client.api_key() == "tok-native", "the native name wins when both are set"


def test_blank_and_whitespace_are_not_a_key(monkeypatch):
    monkeypatch.setenv("ANYSITE_API_KEY", "   ")
    monkeypatch.delenv("ANYSITE_ACCESS_TOKEN", raising=False)
    assert client.api_key() is None


# --- the message ------------------------------------------------------------

def test_a_missing_KEY_is_not_reported_as_a_missing_PACKAGE(monkeypatch):
    monkeypatch.setattr(client, "binary_path", lambda: "/opt/polyrob/venv/bin/anysite")
    monkeypatch.delenv("ANYSITE_API_KEY", raising=False)
    monkeypatch.delenv("ANYSITE_ACCESS_TOKEN", raising=False)
    assert client.missing_requirement() == "key"
    msg = _unavailable_reason()
    assert "no credential is set" in msg
    assert "pip install" not in msg, "this sent the operator to check the wrong thing"


def test_a_missing_PACKAGE_says_so(monkeypatch):
    monkeypatch.setattr(client, "binary_path", lambda: None)
    assert client.missing_requirement() == "binary"
    assert "not installed" in _unavailable_reason()


def test_fully_configured_reports_nothing_missing(monkeypatch):
    monkeypatch.setattr(client, "binary_path", lambda: "/opt/polyrob/venv/bin/anysite")
    monkeypatch.setenv("ANYSITE_API_KEY", "k")
    assert client.missing_requirement() is None


def test_ensure_configured_is_false_without_a_key(monkeypatch):
    """It used to return True for 'binary present, no key', turning a missing
    credential into a call that fails later with the vendor's error."""
    monkeypatch.setattr(client, "binary_path", lambda: "/opt/polyrob/venv/bin/anysite")
    monkeypatch.delenv("ANYSITE_API_KEY", raising=False)
    monkeypatch.delenv("ANYSITE_ACCESS_TOKEN", raising=False)
    assert client.ensure_configured() is False
