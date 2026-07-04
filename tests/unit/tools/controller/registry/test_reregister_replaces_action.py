"""H5: wrap_function had two collision checks. The first logged 'previous registration
will be replaced' and fell through; the SECOND (just before the actual write) logged
'already registered ... skipping' and returned — so a same-tool re-registration silently
discarded the rebuilt action and kept the STALE closure (bound to the old tool instance),
and never busted the per-provider schema cache.
"""
from tools.controller.registry.service import Registry


def test_reregister_same_tool_replaces_action_and_busts_cache():
    r = Registry()

    @r.action("original description", tool="demo")
    def demo_action():
        return "ok"

    # Prime the per-provider schema cache.
    r.get_all_actions_for_provider("openai")
    assert r._provider_schema_cache

    # Re-register the SAME name + tool with a new function/description.
    def demo_action_v2():
        return "ok2"

    r.wrap_function("demo_action", demo_action_v2, "new description", tool="demo")

    # The registry must now hold the NEW registration, not the stale one.
    assert r.registry.actions["demo_action"].description == "new description"
    # And the schema cache must be busted so the next emit reflects the replacement.
    assert r._provider_schema_cache == {}


def test_cross_tool_collision_still_rejected():
    import pytest

    r = Registry(enforce_execution_context=True)

    @r.action("a", tool="toolA")
    def shared_name():
        return "a"

    def other():
        return "b"

    with pytest.raises(ValueError):
        r.wrap_function("shared_name", other, "b", tool="toolB")
