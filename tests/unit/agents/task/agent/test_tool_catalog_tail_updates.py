"""F8 (063 WS-4) — a tool-catalog status flip is a TAIL delta, never a rewrite of
the pinned foundation snapshot.

The catalog sits at foundation [7] and used to be re-rendered in place every
step. Every provider cache serves only the leading bytes two requests share, so
one flipped status (a `load_tool`, an MCP connect, a credential verdict opening
or closing) re-billed the whole conversation behind it. The first render is the
session baseline; a later change appends one `<tool-catalog-update>` message.
"""
from unittest.mock import MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.core.runtime_catalog import refresh_tool_catalog
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from tests.support.prefix_cache import PrefixCacheProbe, serialize_messages


def _mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=4000,
    )


def _catalog(*rows: str) -> str:
    body = "\n".join(f"- {r}" for r in rows)
    return f"<tool-catalog>\nHeader text that never moves.\n{body}\n</tool-catalog>"


_BEFORE = _catalog(
    'filesystem: read and write workspace files [loaded]',
    'web_fetch: fetch a URL [loadable — load_tool("web_fetch")]',
    'email: send mail [gated:unavailable-on-this-deploy — ask the owner]',
)
_AFTER = _catalog(
    'filesystem: read and write workspace files [loaded]',
    'web_fetch: fetch a URL [loaded]',
    'email: send mail [gated:unavailable-on-this-deploy — ask the owner]',
)


def _agent(mm, render):
    from types import SimpleNamespace
    controller = SimpleNamespace(render_tool_catalog=lambda **kw: render())
    return SimpleNamespace(message_manager=mm, controller=controller, _role="root")


def _text(m):
    return m.content if isinstance(m.content, str) else str(m.content)


def _updates(mm):
    # The system prompt names the tag too (the <source-precedence> line), so the
    # pushed messages are the NON-system ones carrying it.
    return [m for m in mm.get_messages()
            if "<tool-catalog-update>" in _text(m) and m.to_dict()["role"] != "system"]


def test_status_flip_keeps_foundation_bytes_and_appends_one_update(monkeypatch):
    monkeypatch.setenv("TOOL_CATALOG_TAIL_UPDATES", "true")
    mm = _mm()
    mm.set_tool_catalog_message(_BEFORE)
    pinned_bytes = _text(mm._tool_catalog_message)
    pinned_obj = mm._tool_catalog_message

    refresh_tool_catalog(_agent(mm, lambda: _AFTER))

    # [7] is untouched — same object AND same bytes.
    assert mm._tool_catalog_message is pinned_obj
    assert _text(mm._tool_catalog_message) == pinned_bytes

    updates = _updates(mm)
    assert len(updates) == 1
    body = _text(updates[0])
    assert "web_fetch: fetch a URL [loaded]" in body
    # Only the flipped tool is named.
    assert "filesystem" not in body
    assert "email" not in body
    assert "This supersedes the earlier catalog lines for these tools." in body


def test_identical_render_pushes_nothing(monkeypatch):
    monkeypatch.setenv("TOOL_CATALOG_TAIL_UPDATES", "true")
    mm = _mm()
    mm.set_tool_catalog_message(_BEFORE)
    agent = _agent(mm, lambda: _BEFORE)
    refresh_tool_catalog(agent)
    refresh_tool_catalog(agent)
    assert _updates(mm) == []


def test_the_same_delta_is_not_pushed_twice(monkeypatch):
    monkeypatch.setenv("TOOL_CATALOG_TAIL_UPDATES", "true")
    mm = _mm()
    mm.set_tool_catalog_message(_BEFORE)
    agent = _agent(mm, lambda: _AFTER)
    refresh_tool_catalog(agent)
    refresh_tool_catalog(agent)
    refresh_tool_catalog(agent)
    assert len(_updates(mm)) == 1


def test_render_exception_pushes_nothing_and_keeps_the_baseline(monkeypatch):
    monkeypatch.setenv("TOOL_CATALOG_TAIL_UPDATES", "true")
    mm = _mm()
    mm.set_tool_catalog_message(_BEFORE)
    pinned_bytes = _text(mm._tool_catalog_message)

    controller = MagicMock()
    controller.render_tool_catalog.side_effect = RuntimeError("registry unavailable")
    from types import SimpleNamespace
    refresh_tool_catalog(SimpleNamespace(message_manager=mm, controller=controller))

    assert _text(mm._tool_catalog_message) == pinned_bytes
    assert _updates(mm) == []


def test_flag_off_restores_the_in_place_rewrite(monkeypatch):
    monkeypatch.setenv("TOOL_CATALOG_TAIL_UPDATES", "false")
    mm = _mm()
    mm.set_tool_catalog_message(_BEFORE)
    refresh_tool_catalog(_agent(mm, lambda: _AFTER))
    assert "web_fetch: fetch a URL [loaded]" in _text(mm._tool_catalog_message)
    assert _updates(mm) == []


def test_a_tool_that_leaves_the_catalog_is_named(monkeypatch):
    monkeypatch.setenv("TOOL_CATALOG_TAIL_UPDATES", "true")
    mm = _mm()
    mm.set_tool_catalog_message(_BEFORE)
    shrunk = _catalog('filesystem: read and write workspace files [loaded]',
                      'web_fetch: fetch a URL [loadable — load_tool("web_fetch")]')
    refresh_tool_catalog(_agent(mm, lambda: shrunk))
    body = _text(_updates(mm)[0])
    assert "email: [no longer listed in the tool catalog]" in body


def test_flip_leaves_the_previous_assembly_as_a_full_prefix(monkeypatch):
    """The cache measurement: after the flip, the earlier request's messages are
    still the leading bytes of the new one."""
    monkeypatch.setenv("TOOL_CATALOG_TAIL_UPDATES", "true")
    mm = _mm()
    mm.set_tool_catalog_message(_BEFORE)
    probe = PrefixCacheProbe()
    probe.record(mm.get_messages_for_llm(consume_ephemeral=False))

    refresh_tool_catalog(_agent(mm, lambda: _AFTER))
    probe.record(mm.get_messages_for_llm(consume_ephemeral=False))

    assert probe.first_divergence() is None, probe.describe_last()
    assert probe.hit_ratio == 1.0, probe.describe_last()
    assert probe.last_prefix_len == probe.previous_len()


def test_flip_under_the_old_rewrite_breaks_the_prefix(monkeypatch):
    """Control case — the defect F8 removes, still reachable with the flag off."""
    monkeypatch.setenv("TOOL_CATALOG_TAIL_UPDATES", "false")
    mm = _mm()
    mm.set_tool_catalog_message(_BEFORE)
    before = serialize_messages(mm.get_messages_for_llm(consume_ephemeral=False))
    refresh_tool_catalog(_agent(mm, lambda: _AFTER))
    after = serialize_messages(mm.get_messages_for_llm(consume_ephemeral=False))
    assert before != after
    from tests.support.prefix_cache import common_prefix_len
    assert common_prefix_len(before, after) < len(before)


def test_no_baseline_yet_makes_the_first_render_the_foundation(monkeypatch):
    """A session whose catalog slot is empty is not given a delta — F8 only ever
    diffs against a render that was actually pinned."""
    monkeypatch.setenv("TOOL_CATALOG_TAIL_UPDATES", "true")
    mm = _mm()
    mm.update_tool_catalog(_BEFORE)
    assert mm._tool_catalog_message is not None
    assert "web_fetch" in _text(mm._tool_catalog_message)
    assert _updates(mm) == []


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False)])
def test_source_precedence_line_follows_the_flag(monkeypatch, value, expected):
    monkeypatch.setenv("TOOL_CATALOG_TAIL_UPDATES", value)
    sp = SystemPrompt.__new__(SystemPrompt)
    sp._catalog_pinned = lambda: True  # the catalog line renders only when pinned (F11)
    text = SystemPrompt._get_source_precedence_content(sp)
    assert ("A later <tool-catalog-update> wins over the opening catalog" in text) is expected
