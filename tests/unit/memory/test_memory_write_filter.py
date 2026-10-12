"""B2 (2026-10-11) — the cross-session write filter.

Prod's `memories` store was ~80 % chatter: `[STRATEGIC CLUE FROM …]` phase
hints, `tool(args)→result` echoes and run-status narration. The fixtures are
real prod rows (scrubbed); the classifier must drop the noise and keep every
finding, owner ruling and decision.
"""
import json
from pathlib import Path

import pytest

from core.sqlite_util import execute_retry
from modules.memory import write_filter as wf
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

_FIX = json.loads((Path(__file__).parent / "fixtures" / "memory_noise_samples.json")
                  .read_text(encoding="utf-8"))["rows"]


def test_fixture_set_is_large_and_mixed():
    kinds = {r["expect"] for r in _FIX}
    assert len(_FIX) >= 50 and kinds == {"drop", "keep", "edit"}


@pytest.mark.parametrize("row", _FIX, ids=lambda r: f'{r["expect"]}:{r["content"][:40]}')
def test_prod_samples(row):
    kept, reasons = wf.filter_content(row["content"])
    if row["expect"] == "drop":
        assert kept == "" and reasons
    elif row["expect"] == "keep":
        assert reasons == [] and kept == row["content"].strip()
    else:
        assert kept == row["kept"] and kept and reasons


@pytest.mark.parametrize("text", [
    "filesystem_read_file(file_path=reports/x.md, offset=8)→[lines 8-15 of 2767]",
    "cronjob_list→Scheduled cron jobs: - `abc` [cancelled]",
    "done(text=All set)→DONE. Task complete",
    "[unverified] shell_run(command=npm install, background=False)→ERROR:command exited 1",
    "Found: holdings for 0xADDR (chain robinhood) / coverage: indexed",
    "[STRATEGIC CLUE FROM DISCOVERY] Discovery: 3 insights. Proceed with verified tools and methods.",
    "Starting the 02:20Z safety monitor run. Plan: STEP 1 verdict set, then compare to baseline.",
    "Starting DM CHECK cron run at 22:00 UTC. Need to read X Chat inbox.",
    "Planning turn done. Now issuing the safety verdict set in parallel.",
    "Run complete.",
    "DM CHECK run complete: 19 conversations, no new thread, all unchanged. Nothing new.",
    "Self-wake confirms the goal already completed: report sent. No pending work remains.",
])
def test_noise_is_dropped(text):
    assert wf.clean(text) == ""


@pytest.mark.parametrize("text", [
    # an owner ruling inside a run-start line survives
    "Starting the 15:00 promo slot. Owner declined the PNL draft and ruled no token shilling.",
    "Owner 2026-10-04 12:18: 'ok cool so what's the plan next i approve everything.'",
    "User requested: 'New release should work daily not every 15 minutes.'",
    # a "done, nothing new" line that explains a refusal is a diagnosis
    "Cycle 04:02 UTC complete. No tranche: the swap guard REFUSED the dry-run because the "
    "wallet pin is not applied; nothing new to report.",
    "Lesson: the reconcile verb refuses files over 1MB — run it on the trimmed extract.",
    "Run complete. reports/rh-weekly-pnl.md written and verified (10,852 bytes) and delivered "
    "to the owner on Telegram with the file attached.",
    "Grep returned no matches — unexpected since file was write-verified at 14692 bytes.",
    # review of 20d839f9f: real prod findings the v1 rules dropped
    "12:20Z run complete through STEP 3. Rate 64,921,027.88 PNL/WETH (-35.1% vs 10:21 "
    "100,000,000) -> 08:21 flag CLEARED, owner notified. Gas 14.434667 ETH fine. Top-10 66.5% "
    "standing unchanged. LP locked 0.0%. History row appended to TOP table.",
    "Starting EXIT RAIL run 2026-09-20 10:03 UTC. Prior run sold DELTA in full (tx 0x540b12ab). "
    "Known standing issues: ledger >1MB, reconcile must run on the trimmed extract.",
    "Planning turn done. Job text says 6551 promo but owner rule 2026-10-07 12:22 STOPPED all "
    "6551 promotion, so this run is POLYROB-only.",
    "Starting SAFETY MONITOR run, read-only, $0 spend.",
])
def test_findings_are_kept(text):
    assert wf.clean(text) == text


def test_echo_block_swallows_tool_output_but_not_the_next_finding():
    content = "\n".join([
        "shell_run(command=npm install, background=False)→ERROR:command exited 1",
        "[stderr]",
        "npm error code EACCES",
        "np. Error: command exited 1",
        "Verified ground truth: x402_pay is STILL gated:money in this session (grant at create).",
    ])
    assert wf.clean(content) == (
        "Verified ground truth: x402_pay is STILL gated:money in this session (grant at create).")


def test_clue_line_dropped_next_finding_kept():
    content = ("[STRATEGIC CLUE FROM COLLECTION] Collection: 4 events. Verify data integrity "
               "and exact counts before processing.\nOwner approved on-chain buying.")
    assert wf.filter_content(content) == ("Owner approved on-chain buying.", ["clue"])


def test_empty_and_none():
    assert wf.filter_content("") == ("", [])
    assert wf.filter_content(None) == ("", [])


# ---- the write seam --------------------------------------------------------

@pytest.fixture()
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


def _rows(provider):
    return [r["content"] for r in execute_retry(
        provider.db_path, "SELECT content FROM memories ORDER BY rowid", fetch="all")]


@pytest.mark.asyncio
async def test_sync_turn_writes_nothing_for_pure_noise(provider):
    got = await provider.sync_turn(
        "task", "cronjob_list→Scheduled cron jobs: none\nPlanning turn done. Now reading.",
        session_id="s1", user_id="u")
    assert got is None and _rows(provider) == []


@pytest.mark.asyncio
async def test_sync_turn_keeps_the_finding_of_a_mixed_drain(provider):
    await provider.sync_turn(
        "task",
        "Starting EXIT RAIL run 2026-09-19 12:07 UTC. Plan: reconcile.\n"
        "Reconcile verdict: 7 open rows, all match chain exactly.",
        session_id="s1", user_id="u")
    assert _rows(provider) == ["Reconcile verdict: 7 open rows, all match chain exactly."]


@pytest.mark.asyncio
async def test_transcript_mode_noise_writes_nothing(provider, monkeypatch):
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "false")
    await provider.sync_turn("task", "Run complete.", session_id="s1", user_id="u")
    assert _rows(provider) == []


@pytest.mark.parametrize("finding", [
    "Owner wants daily digest at 9.",
    "PNL buyback filled at 0.05 ETH.",
    "Decided: skip the 15:00 slot.",
])
def test_short_ruling_or_measurement_ends_the_echo_block(finding):
    """Review 2026-10-11: a short finding after a tool echo was swallowed as
    tool output, so the one-time purge would delete an owner ruling."""
    content = "filesystem_read_file(path=x)→ ok\n" + finding
    assert wf.filter_content(content) == (finding, ["echo"])


def test_short_output_lines_still_belong_to_the_echo():
    content = "web_search(q=pnl)→ 3 results\n1. pnl.xyz\nnpm error code 1234"
    assert wf.clean(content) == ""
