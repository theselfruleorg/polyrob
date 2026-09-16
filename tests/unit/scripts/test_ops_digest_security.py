"""045: the brief's SEC line. Never a confident zero over an unreadable store."""
import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ops_digest",
    pathlib.Path(__file__).resolve().parents[3] / "scripts" / "ops_digest.py")
ops_digest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ops_digest)


def test_section_renders_counts():
    body = ops_digest.assemble_report({
        "security": {"inbound": 412, "denied": 38, "refused": 2, "flagged": 0,
                     "unavailable": ""},
    })
    assert "## Security" in body
    assert "412 inbound" in body
    assert "38 denied" in body


def test_unavailable_store_says_so():
    body = ops_digest.assemble_report({
        "security": {"inbound": 0, "denied": 0, "refused": 0, "flagged": 0,
                     "unavailable": "telemetry_events.db not found"},
    })
    assert "unavailable" in body
    assert "0 inbound" not in body


def test_collect_security_reports_the_reason_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "absent.db"))
    out = ops_digest.collect_security(str(tmp_path), "u1", 1)
    assert out["unavailable"]
    assert out["inbound"] == 0


def test_headline_sec_line_renders_counts():
    line = ops_digest.format_headline({
        "security": {"inbound": 412, "denied": 38, "refused": 2, "flagged": 1,
                     "unavailable": ""},
    })
    assert "SEC" in line
    assert "412 inbound" in line
    assert "38 denied" in line
    assert "1 flagged" in line


def test_headline_sec_line_says_unavailable():
    line = ops_digest.format_headline({
        "security": {"inbound": 0, "denied": 0, "refused": 0, "flagged": 0,
                     "unavailable": "telemetry_events.db not found"},
    })
    assert "SEC" in line
    assert "unavailable" in line
    assert "0 inbound" not in line
