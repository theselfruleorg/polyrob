"""P2-18 (intelligence-polish plan 2026-07-07): a tool method that already carries the
tool name must NOT be double-prefixed at registration.

Tool `perplexity` with method `perplexity_search` used to register as
`perplexity_perplexity_search` (and `anysite_anysite_api`, `goal_goal_list`, …),
differing from the names the prompt/skills teach and leaving only the collision-prone
fuzzy suffix match to rescue calls. Now it registers as `perplexity_search`.
"""
import logging
import threading

from tools.controller.service import Controller
from tools.controller.registry.service import Registry


def _make_controller():
    c = object.__new__(Controller)
    c.logger = logging.getLogger("p2_18")
    c._lock = threading.RLock()
    c._tools = {}
    c._action_list_cache = None
    c._tool_list_cache = None
    c.registry = Registry()
    c.session_id = None
    c.user_id = None
    c.workspace_dir = None
    return c


class _PerplexityTool:
    def get_actions(self):
        async def perplexity_search(params=None):
            return "ok"
        return {"perplexity_search": perplexity_search}


class _FetchTool:
    def get_actions(self):
        async def fetch_url(params=None):
            return "ok"
        return {"fetch_url": fetch_url}


def test_pre_prefixed_method_not_double_prefixed():
    c = _make_controller()
    c.add_tool("perplexity", _PerplexityTool())
    names = set(c.registry.get_action_names())
    assert "perplexity_search" in names
    assert "perplexity_perplexity_search" not in names


def test_unprefixed_method_still_namespaced():
    c = _make_controller()
    c.add_tool("web_fetch", _FetchTool())
    names = set(c.registry.get_action_names())
    # a bare method name still gets the tool namespace
    assert "web_fetch_fetch_url" in names
