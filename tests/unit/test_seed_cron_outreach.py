"""Tests for the proactive owner-outreach cron seeder."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.seed_cron_outreach import build_outreach_job  # noqa: E402


def test_outreach_job_delivers_to_telegram():
    job = build_outreach_job(schedule="30m", model="deepseek/deepseek-v3.2")
    assert job["schedule_spec"] == "30m"
    assert job["payload"]["deliver"] == "telegram"
    # deliver to owner's OWN channel -> no explicit target (no exfiltration)
    assert job["payload"].get("deliver_target") is None
    assert job["payload"]["provider"] == "openrouter"
    assert job["payload"]["model"] == "deepseek/deepseek-v3.2"
    assert job["user_id"] == "rob"


def test_outreach_prompt_is_silent_aware_and_owner_directed():
    job = build_outreach_job(schedule="2h", model="x-ai/grok-4.3")
    task = job["task"].lower()
    assert "[silent]" in task          # opt-out convention is taught
    assert "owner" in task             # it knows who to address
    assert "update" in task or "report" in task
    assert job["schedule_spec"] == "2h"


def test_outreach_tools_are_readonly_local():
    job = build_outreach_job(schedule="30m", model="minimax/minimax-m3")
    for forbidden in ("twitter", "hyperliquid", "polymarket", "x402", "wallet",
                      "code_execution", "browser"):
        assert forbidden not in job["payload"]["tools"]
