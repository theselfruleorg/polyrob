"""Social skills, templates, tools, and capability policy must move together."""
import json
from pathlib import Path

from agents.task.templates import seeded_skills_for
from agents.task.tool_defaults import resolve_toolset
from core.tool_capabilities import TOOL_CAPABILITIES, TOOL_PERMISSIONS


SKILLS = Path(__file__).resolve().parents[4] / "data" / "prompts" / "skills"


def test_x_engagement_routes_both_x_rails_and_current_actions():
    rules = json.loads((SKILLS / "rules.json").read_text())
    triggers = rules["x-engagement"]["triggers"]
    assert {"twitter", "x_browser"} <= set(triggers["tool_ids"])
    assert {
        "twitter_get_dms", "twitter_dm", "twitter_post",
        "x_read_dms", "x_dm", "x_post",
    } <= set(triggers["action_names"])


def test_social_template_has_matching_skills_and_tools():
    assert {"social-discovery", "x-engagement"} <= set(
        seeded_skills_for("social"))
    assert {"anysite", "twitter", "x_browser"} <= set(resolve_toolset("social"))


def test_social_tool_capabilities_preserve_read_write_and_delegation_boundaries():
    for tool in ("twitter", "x_browser"):
        permissions = set(TOOL_PERMISSIONS[tool])
        assert {"network.read", "network.write", "social.post"} <= permissions
        assert "high_impact" in TOOL_CAPABILITIES[tool]
    assert "delegate_blocked" in TOOL_CAPABILITIES["x_browser"]
