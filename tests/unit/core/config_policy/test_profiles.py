"""067 P1: the ONE ordered named-profile table (core/config_policy/profiles.py)."""
import pytest

from core.config_policy import profiles as P


def test_every_member_is_a_known_tool_id_or_message():
    from agents.task.agent.skill_manager import VALID_TOOL_IDS
    unknown = {name: [t for t in ids if t not in VALID_TOOL_IDS and t != "message"]
               for name, ids in P.PROFILES.items()}
    assert not {k: v for k, v in unknown.items() if v}, unknown


def test_profiles_are_ordered_tuples_without_duplicates():
    for name, ids in P.PROFILES.items():
        assert isinstance(ids, tuple), name
        assert ids, name
        assert len(ids) == len(set(ids)), f"{name} repeats an id"
        assert ":" in name, f"{name} must be namespaced (default:/toolset:/grant:/rig:/...)"


def test_colliding_names_are_distinct_profiles():
    # TOOLSETS and RIGS both had research/social/full with different content.
    assert P.profile("toolset:research") != P.profile("rig:research")
    assert P.profile("toolset:social") != P.profile("rig:social")
    assert "rig:full" not in P.PROFILES  # `full` rig = the caller's default


def test_server_default_is_the_full_toolset():
    assert P.PROFILES["default:server"] is P.PROFILES["toolset:full"]
    assert P.PROFILES["default:cli"] is P.PROFILES["toolset:default"]
    assert P.profile("default:server") == P.profile("toolset:full")


def test_profile_drops_a_member_this_install_does_not_provide(monkeypatch):
    """067 P3: a pack's ids stay in the raw table; the resolved profile keeps
    only classified tools and action ids, order kept, quietly."""
    from core import tool_capabilities as tc
    monkeypatch.setattr(tc, "TOOL_CAPABILITIES",
                        {k: v for k, v in tc.TOOL_CAPABILITIES.items()
                         if k not in ("anysite", "perplexity")})
    assert "anysite" in P.PROFILES["rig:research"]
    assert P.profile("rig:research") == tuple(
        t for t in P.PROFILES["rig:research"] if t != "anysite")
    assert "message" in P.profile("rig:research")
    assert P.provided_only(["perplexity", "task", "message"]) == ["task", "message"]


def test_a_pack_withheld_at_phase_two_drops_out(monkeypatch):
    import core.packs.state as st
    monkeypatch.setattr(st, "tool_withheld", lambda t: t == "anysite")
    assert "anysite" not in P.profile("rig:research")


def test_resolve_keeps_order_and_filters_to_installed():
    ids = P.profile("rig:social")
    assert P.resolve("rig:social") == ids
    assert P.resolve("rig:social", installed={"task", "twitter", "nope"}) == ("twitter", "task")
    assert P.resolve("rig:social", installed=[]) == ()


def test_unknown_name_fails_loudly():
    with pytest.raises(KeyError):
        P.profile("toolset:does-not-exist")


def test_names_by_prefix_keep_table_order():
    assert P.names("rig") == ("money_rail", "social", "research", "ops")
    assert P.names("toolset:")[0] == "minimal"


# ── ratchet: a named tool set is spelled ONLY in profiles.py ─────────────────
# The modules whose lists became views (067 P1) plus the default-toolset
# consumers. A list/tuple/set literal naming >=3 known tool ids in one of them
# is a second spelling of a profile — add a profile instead and read it.
_VIEW_MODULES = (
    "agents/task/tool_defaults.py",
    "agents/task/constants.py",
    "core/config_policy/rigs.py",
    "tools/goal_tools.py",
    "agents/task/goals/dispatcher.py",
    "cli/toolset.py",
    "cron/runner.py",
    "surfaces/telegram/interactive_tools.py",
)
# (module, sorted ids) literals that are NOT a named tool set, with the reason.
_ALLOWED_LITERALS = {
    # The host/compute ids goal_create can never self-grant: a capability
    # classification (tool_capabilities `exec`), not a toolset anyone is offered.
    ("tools/goal_tools.py", ("code_execution", "process", "shell")),
}


def test_no_second_spelling_of_a_tool_set():
    import ast
    from pathlib import Path

    from agents.task.agent.skill_manager import VALID_TOOL_IDS
    known = set(VALID_TOOL_IDS) | {"message"}
    root = Path(__file__).resolve().parents[4]
    offenders = []
    for rel in _VIEW_MODULES:
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                continue
            ids = [e.value for e in node.elts
                   if isinstance(e, ast.Constant) and isinstance(e.value, str)
                   and e.value in known]
            if len(ids) >= 3 and (rel, tuple(sorted(ids))) not in _ALLOWED_LITERALS:
                offenders.append(f"{rel}:{node.lineno} {ids}")
    assert not offenders, (
        "tool-id set spelled outside core/config_policy/profiles.py:\n  "
        + "\n  ".join(offenders))
