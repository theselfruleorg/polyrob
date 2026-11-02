"""093 C1/C2 ratchets: every gate has a who/when row, and the generated
operations manual is current and pinned for owner seats."""
import re
import subprocess
import sys
from pathlib import Path

from core.tool_gates import (ACTORS, APPLIES, REMEDIES, REMEDY_ACTORS, STATUS_REASONS,
                             remedy_actor, remedy_key_for_text, remedy_text)

REPO = Path(__file__).resolve().parents[3]


def test_every_remedies_key_has_a_who_when_row():
    missing = sorted(set(REMEDIES) - set(REMEDY_ACTORS))
    assert not missing, f"add a REMEDY_ACTORS row for: {missing}"


def test_every_status_reason_has_a_who_when_row():
    missing = sorted(set(STATUS_REASONS) - set(REMEDY_ACTORS))
    assert not missing, f"add a REMEDY_ACTORS row for: {missing}"


def test_rows_use_the_vocabulary():
    for key, a in REMEDY_ACTORS.items():
        assert a.who in ACTORS and a.when in APPLIES, key


def _resolver_reasons() -> set:
    src = (REPO / "tools" / "tool_disclosure.py").read_text(encoding="utf-8")
    found = set(re.findall(r"""["']gated["'],\s*["']([a-z][a-z-]+)["']""", src))
    gap = re.compile(r"""return\s*\(?\s*["']([a-z]+(?:-[a-z]+)+)["'],\s*(?:remedy_text|f["'])""")
    for path in (REPO / "tools").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "availability_gap" in text or "gap_from_reason" in text:
            found |= set(gap.findall(text))
    # 095 W2: the load path's own "gated:<reason> —" lines share the vocabulary.
    line = re.compile(r"gated:([a-z_]+(?:-[a-z]+)*) —")
    for rel in ("tools/controller/tool_load_report.py", "tools/controller/tool_management.py",
                "tools/controller/owner_ask_action.py"):
        found |= set(line.findall((REPO / rel).read_text(encoding="utf-8")))
    return found


def test_every_reason_the_resolver_returns_is_a_status_reason():
    found = _resolver_reasons()
    assert {"money", "no-mcp-server", "backend-unavailable"} <= found  # the scan works
    extra = sorted(found - set(STATUS_REASONS))
    assert not extra, f"add these to core.tool_gates.STATUS_REASONS + REMEDY_ACTORS: {extra}"


def test_the_remedy_key_reads_back_from_its_rendered_text():
    for key in REMEDIES:
        assert remedy_key_for_text(remedy_text(key, tool="mcp", flag="SOME_FLAG")) == key


def test_no_mcp_server_is_an_owner_verb_on_the_next_turn():
    a = remedy_actor("no-mcp-server", remedy_text("no-mcp-server"))
    assert (a.who, a.when) == ("owner-verb", "next-turn")
    assert a.verb.startswith("/mcp add")


def test_the_generated_manual_is_current():
    r = subprocess.run([sys.executable, str(REPO / "devtools" / "gen_harness_manual.py"),
                        "--check"], capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_manual_carries_the_two_rules():
    text = (REPO / "data/prompts/skills/harness-manual/SKILL.md").read_text(encoding="utf-8")
    assert "Never name a cause you did not read from a tool result." in text
    assert "Never advise a restart unless the remedy says restart." in text


def test_owner_seats_pin_the_manual(monkeypatch):
    from types import SimpleNamespace
    from agents.task.agent.core import construction as c
    from core import autonomy_marker as m
    assert c.OWNER_SEAT_SKILLS == ("harness-manual",)   # ⚠️ a pinned id never changes
    m.mark_owner_seat("sess-owner-093")
    agent = SimpleNamespace(orchestrator=SimpleNamespace(session_id="sess-owner-093"))
    assert c.owner_seat_skill_ids(agent) == ["harness-manual"]
    other = SimpleNamespace(orchestrator=SimpleNamespace(session_id="sess-room-093"))
    assert c.owner_seat_skill_ids(other) == []
    # 095 W2: a goal or cron run pins it too; a leaf of one does not.
    m.mark_autonomous("sess-cron-093", cron_job_id="job-093")
    cron = SimpleNamespace(orchestrator=SimpleNamespace(session_id="sess-cron-093"))
    assert c.owner_seat_skill_ids(cron) == ["harness-manual"]
    m.mark_autonomous("sess-goal-093", goal_id="goal-093")
    goal = SimpleNamespace(orchestrator=SimpleNamespace(session_id="sess-goal-093"))
    assert c.owner_seat_skill_ids(goal) == ["harness-manual"]
    leaf = SimpleNamespace(orchestrator=goal.orchestrator, _is_sub_agent=True)
    assert c.owner_seat_skill_ids(leaf) == []


def test_the_manual_carries_both_tables():
    from core.tool_gates import REFUSAL_KINDS
    text = (REPO / "data/prompts/skills/harness-manual/SKILL.md").read_text(encoding="utf-8")
    assert "## Gate — who · when · fix" in text and "## Refusal — who" in text
    for kind in REFUSAL_KINDS:
        assert f"- {kind}: " in text, kind


def test_the_pinned_skill_loads_through_the_skill_manager():
    from agents.task.agent.skill_manager import get_skill_manager
    got = get_skill_manager().get_pinned_skills(seeded_skill_ids=["harness-manual"])
    assert [s.skill_id for s in got][:1] == ["harness-manual"]
