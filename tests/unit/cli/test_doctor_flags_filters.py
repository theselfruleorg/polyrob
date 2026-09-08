"""030 C5 — `doctor --flags` filters (`--group` / `--search` / `--changed`).

The gap: the flags report printed all ~487 flags (~504 lines) with no way to
narrow it — the one complete view was unusable. The filters combine (AND),
apply to text and `--json` alike, keep group headers for surviving rows, and
an empty match prints an honest one-line "no flags match" message (exit 0).
Any filter implies `--flags`.
"""
import json

import pytest
from click.testing import CliRunner

from cli.commands.doctor import doctor, flags_report


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AUTONOMY_MODE", "AUTONOMY_POSTURE", "POLYROB_LOCAL", "ROB_LOCAL",
                "AGENT_COMPUTE_POSTURE", "PAYMENT_APPROVAL_MODE",
                "MEMORY_TOOL_ENABLED", "CRON_ENABLED"):
        monkeypatch.delenv(var, raising=False)


def _headers(lines):
    return [ln for ln in lines if ln.startswith("## ")]


def _flag_rows(lines):
    return [ln for ln in lines if ln.startswith("  ")]


# --- --group -----------------------------------------------------------------

def test_group_filter_keeps_only_matching_group():
    lines = flags_report({}, group="memory")
    assert _headers(lines) == ["## Memory"]
    assert any("MEMORY_BACKEND" in ln for ln in _flag_rows(lines))


def test_group_filter_is_case_insensitive():
    assert _headers(flags_report({}, group="MEMORY")) == ["## Memory"]


# --- --search ----------------------------------------------------------------

def test_search_filter_narrows_to_matching_flag_names():
    lines = flags_report({}, search="memory_tool")
    rows = _flag_rows(lines)
    assert rows, "expected MEMORY_TOOL_* rows"
    assert all("MEMORY_TOOL" in ln for ln in rows)
    # the surviving rows keep their group header
    assert _headers(lines) == ["## Memory"]


def test_group_and_search_combine_as_and():
    lines = flags_report({}, group="tools", search="cron")
    rows = _flag_rows(lines)
    assert rows
    assert all("CRON" in ln for ln in rows)
    assert all("code-exec" in h for h in _headers(lines))


# --- --changed ---------------------------------------------------------------

def test_changed_shows_env_set_flag_and_hides_untouched_defaults(monkeypatch):
    monkeypatch.setenv("MEMORY_TOOL_ENABLED", "true")
    lines = flags_report({"MEMORY_TOOL_ENABLED": "true"}, changed=True)
    rows = _flag_rows(lines)
    assert any("MEMORY_TOOL_ENABLED" in ln for ln in rows)
    # untouched defaults are hidden, and only groups with matches keep a header
    assert not any("CRON_ENABLED" in ln for ln in rows)
    assert _headers(lines) == ["## Memory"]


def test_changed_hides_env_value_equal_to_default():
    # An env value that equals the resolved default is not a change.
    lines = flags_report({"MEMORY_TOOL_ENABLED": "false"}, changed=True)
    assert not any("MEMORY_TOOL_ENABLED" in ln for ln in _flag_rows(lines))


def test_changed_always_includes_frozen_inert_rows(monkeypatch):
    # The pytest process froze AGENT_COMPUTE_POSTURE=0 at import; a differing
    # env value renders as frozen/INERT — always interesting under --changed.
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    lines = flags_report({"AGENT_COMPUTE_POSTURE": "2"}, changed=True)
    row = next(ln for ln in _flag_rows(lines) if "AGENT_COMPUTE_POSTURE" in ln)
    assert "INERT" in row


# --- empty match -------------------------------------------------------------

def test_no_match_prints_one_honest_line():
    lines = flags_report({}, search="zqxwv-no-such-flag")
    assert lines == ["no flags match the given --group/--search/--changed filters"]


def test_no_match_exits_zero_via_cli():
    res = CliRunner().invoke(doctor, ["--flags", "--search", "zqxwv-no-such-flag"])
    assert res.exit_code == 0, res.output
    assert "no flags match" in res.output


# --- CLI wiring --------------------------------------------------------------

def test_filter_option_implies_flags():
    res = CliRunner().invoke(doctor, ["--search", "memory_backend"])
    assert res.exit_code == 0, res.output
    assert "resolved flags" in res.output
    assert "MEMORY_BACKEND" in res.output
    # the plain doctor report did not run
    assert "resolved provider/model:" not in res.output


def test_json_output_is_filtered():
    res = CliRunner().invoke(doctor, ["--flags", "--search", "memory_backend", "--json"])
    assert res.exit_code == 0, res.output
    report = json.loads(res.output)["report"]
    rows = [ln for ln in report if ln.startswith("  ")]
    assert rows
    assert all("MEMORY_BACKEND" in ln for ln in rows)


def test_unfiltered_report_unchanged():
    # No filter -> the legacy full dump (hundreds of rows, many groups).
    lines = flags_report({})
    assert len(_headers(lines)) > 5
    assert len(_flag_rows(lines)) > 100
