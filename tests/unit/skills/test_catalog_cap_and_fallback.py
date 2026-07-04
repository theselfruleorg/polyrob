"""Task 9 (SK-F1/SK-F3): skill catalog cap + load_skill disk fallback + eager-path
usage bump.

Bug: the catalog call capped at ``max_skills=20`` while the builtin library has 23
rules and every authored skill defaults to priority 6 (the cut band) — a cut skill
was never in ``_session_skills`` and ``load_skill`` resolved ONLY from
``_session_skills``, so cut skills were undiscoverable AND unloadable. Separately,
with ``SKILL_PROGRESSIVE_DISCLOSURE=false`` (eager full-body injection, no
``load_skill`` call), usage was never bumped, so the curator archived actively-used
authored skills as "never used."
"""
import json
import logging
from pathlib import Path

import pytest

from tools.controller._helpers import build_load_skill_result
from tools.controller.registry.service import Registry
from tools.controller.service import Controller
from agents.task.agent.skill_manager import SkillManager
from agents.task.agent.core.construction import _bump_eager_skill_usage
from modules.skills.skill_usage import SkillUsageStore


# =====================================================================
# (a) catalog cap — the priority-6 tail must fit at max_skills=50
# =====================================================================

def test_catalog_includes_priority6_tail():
    """web-scraping/lead-research are real priority-6 builtin rules (verified against
    data/prompts/skills/rules.json) that the old default cap(20) could cut. At the
    new call-site cap of 50 the full 21-entry auto-activatable library fits."""
    sm = SkillManager()
    ids = [s.skill_id for s in sm.get_catalog_skills(user_id="u1", max_skills=50)]
    assert "web-scraping" in ids
    assert "lead-research" in ids


def test_catalog_cap_20_used_to_cut_web_scraping():
    """Sanity/regression anchor: proves the bug was real — the OLD default (20)
    still cuts the last priority-6 entry (web-scraping sorts last: priority 6,
    alphabetically after lead-research/market-research-brief)."""
    sm = SkillManager()
    ids = [s.skill_id for s in sm.get_catalog_skills(user_id="u1")]  # default max_skills=20
    assert "web-scraping" not in ids


# =====================================================================
# (b) load_skill disk fallback
# =====================================================================

def _make_manager(tmp_path: Path, rules: dict, bodies: dict) -> SkillManager:
    (tmp_path / "rules.json").write_text(json.dumps(rules))
    for sid, body in bodies.items():
        d = tmp_path / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body)
    return SkillManager(skills_dir=tmp_path)


def _bare_controller():
    c = object.__new__(Controller)
    c.logger = logging.getLogger("catalog-cap-fallback-test")
    c.registry = Registry()
    c.output_model = None
    c.user_id = "tenant-A"
    c.session_id = "s1"
    c._session_skills = {}
    # No-op the sibling registration methods called at the tail of
    # _register_default_actions — irrelevant to this test and each pulls in its
    # own unrelated attribute requirements (memory backends, self-context, etc.).
    for _name in (
        "_register_session_search_action",
        "_register_memory_tool_action",
        "_register_recent_activity_action",
        "_register_skill_manage_action",
        "_register_self_context_manage_action",
        "_register_insights_action",
        "_ensure_normalize_path_exists",
        "_register_subtask_action",
    ):
        setattr(c, _name, lambda: None)
    return c


async def _call_load_skill(c, skill_id):
    action = c.registry.registry.actions["load_skill"]
    params = action.param_model(skill_id=skill_id)
    return await action.function(params, execution_context=None)


@pytest.mark.asyncio
async def test_load_skill_disk_fallback_on_session_miss(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "true")
    sm = _make_manager(tmp_path, {"web-scraping": {"priority": 6}},
                       {"web-scraping": "# Web Scraping\n\nBody instructions."})
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)

    c = _bare_controller()
    assert c._session_skills == {}  # not preloaded (e.g. cut by the catalog cap)
    c._register_default_actions()

    res = await _call_load_skill(c, "web-scraping")
    assert not res.error
    assert "<skill" in res.extracted_content
    assert "Body instructions." in res.extracted_content
    assert res.metadata.get("skill_loaded") == "web-scraping"


@pytest.mark.asyncio
async def test_load_skill_fallback_bump_uses_resolved_id_not_raw(monkeypatch, tmp_path):
    """SK-F1 (IMPORTANT): bump_load must record the validated/stripped id actually
    resolved and loaded, not the raw (possibly quote/whitespace-padded) params.skill_id
    — otherwise curator usage metrics fragment across two different keys for the
    same skill."""
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "true")
    sm = _make_manager(tmp_path, {"web-scraping": {"priority": 6}},
                       {"web-scraping": "# Web Scraping\n\nBody instructions."})
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)

    store = SkillUsageStore(str(tmp_path / "skill_usage.db"))
    monkeypatch.setattr("modules.skills.skill_usage.get_skill_usage_store", lambda: store)

    c = _bare_controller()
    c._register_default_actions()

    # Raw, quote-wrapped id as an LLM might emit it.
    res = await _call_load_skill(c, '"web-scraping"')
    assert not res.error
    assert res.metadata.get("skill_loaded") == "web-scraping"
    assert store.get_usage("web-scraping", "tenant-A")["load_count"] == 1
    # The raw (unstripped) id must NOT have been used as the metrics key.
    assert store.get_usage('"web-scraping"', "tenant-A").get("load_count", 0) == 0


@pytest.mark.asyncio
async def test_load_skill_unknown_id_still_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "true")
    sm = _make_manager(tmp_path, {}, {})
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)

    c = _bare_controller()
    c._register_default_actions()

    res = await _call_load_skill(c, "totally-not-a-skill")
    assert res.error
    assert "Unknown skill_id" in res.error


@pytest.mark.asyncio
async def test_load_skill_fallback_rejects_path_traversal_id(monkeypatch, tmp_path):
    """SK-F1 (CRITICAL): the disk fallback must not let a crafted skill_id escape
    the requesting tenant's directory. A skill_id containing '..' could otherwise
    join into `user_<other-uid>/<skill_id>` and read another tenant's content."""
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "true")
    sm = _make_manager(tmp_path, {}, {})
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)

    # Another tenant's skill, sitting right next to tenant-A's own user dir.
    secret_dir = tmp_path / "user_other-tenant" / "secret-skill"
    secret_dir.mkdir(parents=True)
    (secret_dir / "SKILL.md").write_text("# Secret\n\nOther tenant's confidential content.")

    c = _bare_controller()
    assert c.user_id == "tenant-A"
    c._register_default_actions()

    # Attacking id: from within user_tenant-A/, '..' climbs back up to the
    # user-dirs root and back down into the other tenant's directory.
    res = await _call_load_skill(c, "../user_other-tenant/secret-skill")
    assert res.error
    assert "Unknown skill_id" in res.error
    assert "confidential" not in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_load_skill_fallback_rejects_absolute_path_id(monkeypatch, tmp_path):
    """SK-F1: an absolute-path-shaped skill_id must also be rejected, not joined
    straight through (pathlib's `/` operator lets an absolute right-hand side
    override the left-hand base entirely)."""
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "true")
    sm = _make_manager(tmp_path, {}, {})
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)

    secret_file = tmp_path / "outside-secret" / "SKILL.md"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("# Secret\n\nAbsolute-path escape content.")

    c = _bare_controller()
    c._register_default_actions()

    res = await _call_load_skill(c, str(secret_file.parent))
    assert res.error
    assert "Unknown skill_id" in res.error
    assert "escape content" not in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_load_skill_pending_draft_not_loadable_via_fallback(monkeypatch, tmp_path):
    """A `.pending/` quarantined draft lives at a different path than the active
    user-skill path _load_skill_content resolves — the fallback must NOT surface it."""
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "true")
    sm = _make_manager(tmp_path, {}, {})
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)

    # Only the .pending/ draft exists — no active user_<uid>/<skill_id>/SKILL.md.
    pending_dir = tmp_path / "user_tenant-A" / ".pending" / "draft-skill"
    pending_dir.mkdir(parents=True)
    (pending_dir / "SKILL.md").write_text("# Draft\n\nUnreviewed content.")

    c = _bare_controller()
    c._register_default_actions()

    res = await _call_load_skill(c, "draft-skill")
    assert res.error
    assert "Unknown skill_id" in res.error


# =====================================================================
# (c) eager-path usage bump (SK-F3)
# =====================================================================

def test_eager_bump_records_usage_for_matched_skills(monkeypatch, tmp_path):
    store = SkillUsageStore(str(tmp_path / "skill_usage.db"))
    monkeypatch.setattr("modules.skills.skill_usage.get_skill_usage_store", lambda: store)

    matched = [SimpleSkillLike("authored-skill"), SimpleSkillLike("another-skill")]
    _bump_eager_skill_usage(matched, "tenant-A", logging.getLogger("test"))

    assert store.get_usage("authored-skill", "tenant-A")["load_count"] == 1
    assert store.get_usage("another-skill", "tenant-A")["load_count"] == 1


def test_eager_bump_noop_for_anonymous_user(monkeypatch, tmp_path):
    store = SkillUsageStore(str(tmp_path / "skill_usage.db"))
    monkeypatch.setattr("modules.skills.skill_usage.get_skill_usage_store", lambda: store)

    _bump_eager_skill_usage([SimpleSkillLike("authored-skill")], None, logging.getLogger("test"))

    assert store.get_usage("authored-skill", "").get("load_count", 0) == 0


class SimpleSkillLike:
    def __init__(self, skill_id):
        self.skill_id = skill_id
