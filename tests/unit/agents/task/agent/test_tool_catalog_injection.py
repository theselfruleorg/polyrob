"""S1 (dynamic tool rig): the <tool-catalog> is pinned as a TOOL_CATALOG-origin
foundation message (like skills/self-context/project-context), NOT embedded in the
system prompt — keeps the system prompt stable/cacheable.
"""
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import MessageOrigin


def _mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=4000,
    )


def _text(m):
    return m.content if isinstance(m.content, str) else str(m.content)


def test_tool_catalog_injected_into_llm_messages_not_system_prompt():
    mm = _mm()
    mm.set_tool_catalog_message("<tool-catalog>\n- web_fetch: x [loadable]\n</tool-catalog>")
    msgs = mm.get_messages_for_llm()

    assert "web_fetch: x [loadable]" not in _text(msgs[0])

    catalog_msgs = [m for m in msgs if "available-tools" in _text(m)]
    assert len(catalog_msgs) == 1
    assert "web_fetch: x [loadable]" in _text(catalog_msgs[0])
    assert catalog_msgs[0].to_dict()["role"] == "user"


def test_make_control_message_tool_catalog_origin():
    from modules.llm.messages import make_control_message
    m = make_control_message("catalog here", MessageOrigin.TOOL_CATALOG)
    assert m.origin == MessageOrigin.TOOL_CATALOG
    assert "<available-tools>" in m.content


def test_no_catalog_block_when_unset():
    mm = _mm()
    msgs = mm.get_messages_for_llm()
    assert not [m for m in msgs if "available-tools" in _text(m)]


def test_set_empty_catalog_message_is_noop():
    mm = _mm()
    mm.set_tool_catalog_message("")
    msgs = mm.get_messages_for_llm()
    assert not [m for m in msgs if "available-tools" in _text(m)]


def test_catalog_also_in_get_messages_foundation():
    mm = _mm()
    mm.set_tool_catalog_message("<tool-catalog>CATALOG_SENTINEL</tool-catalog>")
    msgs = mm.get_messages()
    assert any("CATALOG_SENTINEL" in _text(m) for m in msgs)


def test_live_refresh_replaces_catalog_after_tool_load():
    from types import SimpleNamespace
    from agents.task.agent.core.runtime_catalog import refresh_tool_catalog
    mm = _mm()
    mm.set_tool_catalog_message("<tool-catalog>fixture: loadable</tool-catalog>")
    controller = SimpleNamespace(render_tool_catalog=lambda **kw: "<tool-catalog>fixture: loaded</tool-catalog>")
    refresh_tool_catalog(SimpleNamespace(message_manager=mm, controller=controller, _role="root"))
    catalogs = [m for m in mm.get_messages() if "available-tools" in _text(m)]
    assert len(catalogs) == 1 and "fixture: loaded" in _text(catalogs[0])
    pinned = mm._tool_catalog_message
    refresh_tool_catalog(SimpleNamespace(message_manager=mm, controller=controller))
    assert mm._tool_catalog_message is pinned  # unchanged snapshot is not re-tokenized


def test_refresh_failure_does_not_leave_a_stale_capability_claim():
    from types import SimpleNamespace
    from agents.task.agent.core.runtime_catalog import refresh_tool_catalog
    mm = _mm()
    mm.set_tool_catalog_message("<tool-catalog>fixture: loaded</tool-catalog>")
    controller = MagicMock()
    controller.render_tool_catalog.side_effect = RuntimeError("registry unavailable")
    refresh_tool_catalog(SimpleNamespace(message_manager=mm, controller=controller))
    assert "snapshot unavailable" in mm._tool_catalog_message.content
    assert "fixture: loaded" not in mm._tool_catalog_message.content
