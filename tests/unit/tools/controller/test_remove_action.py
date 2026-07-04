"""Regression: Registry.remove_action actually unregisters (revocation was a no-op).

Controller.remove_tool probed `hasattr(self.registry, 'remove_action')`, which was
always False, so a "removed" tool's actions stayed registered and callable. This
verifies the method exists, removes the action, busts the schema cache, and that
remove_tool follows through.
"""
from tools.controller.registry.service import Registry


def _reg():
    r = Registry()

    @r.action("does a thing", tool="demo")
    def demo_action():
        return "ok"

    return r


def test_remove_action_unregisters():
    r = _reg()
    assert "demo_action" in r.registry.actions
    assert r.remove_action("demo_action") is True
    assert "demo_action" not in r.registry.actions
    # Idempotent / honest about missing names.
    assert r.remove_action("demo_action") is False
    assert r.remove_action("never_existed") is False


def test_remove_action_busts_schema_cache():
    r = _reg()
    # Prime the per-provider schema cache.
    before = r.get_all_actions_for_provider("openai")
    assert any(
        getattr(a, "name", None) == "demo_action" or "demo_action" in str(a)
        for a in before
    ), "demo_action should be in the generated schema before removal"
    assert r._provider_schema_cache  # cache populated

    r.remove_action("demo_action")
    assert r._provider_schema_cache == {}  # cache busted

    after = r.get_all_actions_for_provider("openai")
    assert not any(
        getattr(a, "name", None) == "demo_action" or '"demo_action"' in str(a)
        for a in after
    ), "demo_action must not be emitted after removal"
