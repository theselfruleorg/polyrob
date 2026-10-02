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


def test_sec_line_carries_trips_and_scan_verdict():
    line = ops_digest.format_security({
        "inbound": 5, "denied": 1, "refused": 0, "flagged": 0, "unavailable": "",
        "rate_limited": 2, "scan": "incomplete (pip_audit not run)"})
    assert "2 rate-limited" in line
    assert "scan incomplete (pip_audit not run)" in line


def test_report_names_unreadable_chat_spend():
    body = ops_digest.assemble_report({"security": {
        "inbound": 1, "denied": 0, "refused": 0, "flagged": 0, "unavailable": "",
        "top_chats_by_volume": [("telegram:-100", 4)],
        "spend_unavailable": "FileNotFoundError: surfaces.db not found"}})
    assert "telegram:-100 (4)" in body
    assert "chat spend: unavailable" in body
