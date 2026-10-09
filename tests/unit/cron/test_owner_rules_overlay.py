"""A cron run opens with the owner rules NEWER than the job's own text.

Prod 2026-10-05: at 17:56 the owner said "Approve posts with me"; Rob wrote it
to owner.md. At 18:01 the PROMO cron run had that rule in its foundation
context and posted anyway — its task text said "post one; no owner message on
a routine slot", and the model followed the job. A rule the owner sets after a
job was written must reach that job's run as an explicit override.
"""
from datetime import datetime

import pytest

from cron.jobs import CronJob
from cron.owner_rules_overlay import newer_owner_rules, owner_rules_overlay
from cron.runner import make_agent_runner

DOC = """APPROVE POSTS WITH OWNER (2026-10-05 17:56): owner said "Approve posts with me." [from: owner said 2026-10-05]

PNL FIRST, 6551 SECOND (2026-10-05 17:56): owner said "We should shill pnl token first." [from: owner said 2026-10-05]

OLD RULE (2026-09-23 03:28): owner ruled something long ago. [from: owner said 2026-09-23]

UNDATED RULE with no stamp at all.

## Superseded
- RETIRED RULE (2026-10-05): this one no longer steers. [from: owner said 2026-10-05]
"""


def test_only_active_rules_dated_on_or_after_the_job_are_returned():
    rules = newer_owner_rules(DOC, datetime(2026, 10, 4, 7, 38))
    assert [r.split(" (")[0] for r in rules] == [
        "APPROVE POSTS WITH OWNER", "PNL FIRST, 6551 SECOND"]


def test_nothing_newer_means_no_overlay():
    assert newer_owner_rules(DOC, datetime(2026, 10, 6, 9, 0)) == []
    assert owner_rules_overlay(DOC, datetime(2026, 10, 6, 9, 0)) == ""


def test_no_created_at_or_no_doc_is_inert():
    assert owner_rules_overlay(DOC, None) == ""
    assert owner_rules_overlay("", datetime(2026, 10, 4)) == ""


def test_overlay_states_the_override_and_carries_the_rules():
    block = owner_rules_overlay(DOC, datetime(2026, 10, 4, 7, 38))
    assert "OVERRIDE" in block
    assert "2026-10-04" in block  # names when the job text was written
    assert "Approve posts with me" in block
    assert "OLD RULE" not in block and "RETIRED RULE" not in block


def test_overlay_is_bounded():
    many = "\n".join(f"RULE {i} (2026-10-05): x{'y' * 900} [from: owner said 2026-10-05]"
                     for i in range(20))
    block = owner_rules_overlay(many, datetime(2026, 10, 1))
    assert block.count("\n- ") <= 8
    assert len(block) < 8 * 600 + 800


def test_the_cap_keeps_the_newest_rules_whatever_the_doc_order():
    old_first = "\n".join(
        [f"OLD {i} (2026-10-01): x [from: owner said 2026-10-01]" for i in range(10)]
        + ["NEWEST (2026-10-05): y [from: owner said 2026-10-05]"])
    rules = newer_owner_rules(old_first, datetime(2026, 9, 30))
    assert len(rules) == 8 and rules[0].startswith("NEWEST")


@pytest.mark.asyncio
async def test_cron_run_task_opens_with_the_newer_owner_rules(tmp_path):
    from core.instance import resolve_instance_id, self_tier_root
    root = self_tier_root(tmp_path, "u1", resolve_instance_id())
    root.mkdir(parents=True)
    (root / "owner.md").write_text(DOC, encoding="utf-8")
    captured = {}

    class _TaskAgent:
        async def create_session(self, user_id, request):
            captured["request"] = request
            return {"id": "s1"}

        async def run_session(self, user_id, session_id):
            return "ok"

    runner = make_agent_runner(_TaskAgent(), data_dir=str(tmp_path))
    job = CronJob(id="j", task="PROMO: post one post.", schedule_spec="1h", user_id="u1",
                  next_run_at=None, payload={"provider": "anthropic"},
                  created_at=datetime(2026, 10, 4, 7, 38))
    assert await runner(job) is True
    task = captured["request"]["task"]
    assert task.index("Approve posts with me") < task.index("PROMO: post one post.")
    assert task.rstrip().endswith("PROMO: post one post.")
