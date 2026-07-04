"""P9 pass-9 — ResourceMixin extracted from service.py."""
import logging

from agents.task.agent.core.resources import ResourceMixin


def test_agent_composes_resource_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, ResourceMixin)
    for m in ("_initialize_memory_management", "cleanup_memory", "get_browser_context"):
        assert getattr(Agent, m).__qualname__.startswith("ResourceMixin")


class _Host(ResourceMixin):
    def __init__(self):
        self.logger = logging.getLogger("resource-test")


def test_initialize_memory_management_sets_bounded_collections():
    h = _Host()
    h._initialize_memory_management()
    assert hasattr(h, "_telemetry_requests") and hasattr(h, "_file_references")


def test_cleanup_memory_returns_stats_dict():
    h = _Host()
    h._initialize_memory_management()
    stats = h.cleanup_memory(force=True)
    assert isinstance(stats, dict)
    assert "gc_collected" in stats
