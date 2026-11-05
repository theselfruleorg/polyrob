"""The install count: one anonymous signal, said out loud, and easy to turn off."""
import os
import stat
from pathlib import Path

import pytest

from tests.install.test_installer_packs import _run


@pytest.fixture
def sent(tmp_path, monkeypatch):
    """A curl on PATH that records its arguments. Nothing reaches the network."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    log = tmp_path / "curl.log"
    curl = bin_dir / "curl"
    curl.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{log}"\nexit "${{CURL_RC:-0}}"\n')
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    return log


def _lines(log: Path):
    return log.read_text().splitlines() if log.exists() else []


def test_fresh_install_sends_only_version_os_and_method(tmp_path, sent):
    res, *_ = _run(tmp_path)
    assert res.returncode == 0, res.stderr
    lines = _lines(sent)
    assert "https://polyrob.dev/api/install-event" in lines
    body = lines[lines.index("-d") + 1]
    fields = dict(pair.split("=", 1) for pair in body.split("&"))
    assert set(fields) == {"event", "version", "os", "method"}
    assert fields["event"] == "completed"
    assert fields["method"] == "source"
    assert fields["os"] in {"linux", "darwin"}
    assert str(tmp_path) not in body


def test_the_installer_says_so_before_it_sends(tmp_path, sent):
    res, *_ = _run(tmp_path)
    assert "one anonymous count" in res.stdout
    assert "--no-telemetry" in res.stdout
    assert res.stdout.index("one anonymous count") < res.stdout.index("Python ")


@pytest.mark.parametrize("args,env", [
    (("--no-telemetry",), {}),
    ((), {"DO_NOT_TRACK": "1"}),
    ((), {"CI": "true"}),
])
def test_opting_out_sends_nothing_and_stays_quiet(tmp_path, sent, monkeypatch, args, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    res, *_ = _run(tmp_path, *args)
    assert res.returncode == 0, res.stderr
    assert _lines(sent) == []
    assert "anonymous count" not in res.stdout


def test_do_not_track_zero_is_not_an_opt_out(tmp_path, sent, monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "0")
    res, *_ = _run(tmp_path)
    assert res.returncode == 0, res.stderr
    assert "https://polyrob.dev/api/install-event" in _lines(sent)


def test_a_re_run_sends_nothing(tmp_path, sent):
    home_bin = tmp_path / "home" / ".local" / "bin"
    home_bin.mkdir(parents=True)
    (home_bin / "polyrob").write_text("#!/bin/sh\n")
    res, *_ = _run(tmp_path)
    assert res.returncode == 0, res.stderr
    assert _lines(sent) == []


def test_a_failed_send_does_not_stop_the_install(tmp_path, sent, monkeypatch):
    monkeypatch.setenv("CURL_RC", "7")
    res, *_ = _run(tmp_path)
    assert res.returncode == 0, res.stderr
    assert "POLYROB is installed" in res.stdout
