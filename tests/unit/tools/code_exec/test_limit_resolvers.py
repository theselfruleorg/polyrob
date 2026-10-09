"""CODE_EXEC_MAX_TIMEOUT_SEC / CODE_EXEC_MAX_OUTPUT_BYTES: one resolver each
(tools/code_exec/limits.py), read by every backend and the kernel."""

import pathlib
import re

import pytest

from tools.code_exec import limits


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("CODE_EXEC_MAX_TIMEOUT_SEC", "CODE_EXEC_MAX_OUTPUT_BYTES", "SHELL_MAX_TIMEOUT_SEC"):
        monkeypatch.delenv(k, raising=False)


def test_defaults():
    assert limits.max_output_bytes() == 100000
    assert limits.max_timeout_sec() == 30.0
    assert limits.max_timeout_sec(dev_mode=True) == 600.0


def test_explicit_timeout_wins_in_both_modes(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_MAX_TIMEOUT_SEC", "45")
    assert limits.max_timeout_sec() == 45.0
    assert limits.max_timeout_sec(dev_mode=True) == 45.0
    assert limits.exec_timeout_cap(45.0, 900.0) == 45.0


@pytest.mark.parametrize("raw", ["abc", "nan", "inf"])
def test_bad_timeout_falls_back(monkeypatch, raw):
    monkeypatch.setenv("CODE_EXEC_MAX_TIMEOUT_SEC", raw)
    assert limits.max_timeout_sec() == 30.0
    assert limits.max_timeout_sec(dev_mode=True) == 600.0


def test_bad_output_bytes_falls_back(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_MAX_OUTPUT_BYTES", "lots")
    assert limits.max_output_bytes() == 100000
    monkeypatch.setenv("CODE_EXEC_MAX_OUTPUT_BYTES", "2048")
    assert limits.max_output_bytes() == 2048


def test_no_second_parse_of_the_limit_env_vars():
    root = pathlib.Path(__file__).resolve().parents[4] / "tools" / "code_exec"
    pat = re.compile(r"getenv\(\s*[\"']CODE_EXEC_MAX_(?:TIMEOUT_SEC|OUTPUT_BYTES)|_env\(\s*[\"']CODE_EXEC_MAX_")
    offenders = [str(p) for p in root.rglob("*.py")
                 if p.name != "limits.py" and pat.search(p.read_text())]
    assert not offenders, offenders
