"""Tests for the ``/context`` REPL slash-command handler
(cli/ui/commands/h_context.py) — owner-UX P1 T9.

``render_context_breakdown(message_manager) -> str`` is driven directly as a
pure function (the established ``h_*`` pattern — see ``test_h_config.py``),
built over a real ``MessageManager`` via the same fixture pattern as
``agents/task/agent/message_manager/tests.py`` (a minimal fake chat model —
the manager only reads ``model_name`` off it). The brief's Step-1 test is
verbatim; the rest extend coverage for the "no session" line, graceful
degradation over a manager stub missing accessors, percentage-against-total
fallback, and the registered ``/context`` handler wrapper.
"""
from __future__ import annotations

import pytest


class _FakeChatModel:
    """Minimal LLM stand-in — MessageManager only reads ``model_name`` off it."""

    def __init__(self, model_name: str):
        self.model_name = model_name


@pytest.fixture
def mm_with_history():
    """A real MessageManager with a system prompt, one skill message, and two
    conversational turns — enough to populate the system/skills/history slots."""
    from agents.task.agent.message_manager.service import MessageManager
    from agents.task.agent.prompts import SystemPrompt

    mm = MessageManager(
        llm=_FakeChatModel("gpt-5"),
        task="Test task",
        action_descriptions="Test actions",
        system_prompt_class=SystemPrompt,
        max_input_tokens=1000,
        image_tokens=800,
    )
    mm.set_skill_message("Use tool X to accomplish Y.")
    mm.add_human_message("hello there")
    mm.add_human_message("how are you today")
    return mm


# ---------------------------------------------------------------------------
# Brief's Step 1 test (verbatim contract)
# ---------------------------------------------------------------------------


def test_render_names_slots_and_totals(mm_with_history):
    from cli.ui.commands.h_context import render_context_breakdown
    out = render_context_breakdown(mm_with_history)
    for label in ("system", "skills", "history", "total"):
        assert label in out.lower()
    assert "%" in out


# ---------------------------------------------------------------------------
# Percentages + footer
# ---------------------------------------------------------------------------


def test_percent_against_known_context_limit(mm_with_history):
    from cli.ui.commands.h_context import render_context_breakdown

    out = render_context_breakdown(mm_with_history)
    assert "context limit: 1,000 tokens" in out


def test_populated_slots_only_unpopulated_omitted(mm_with_history):
    """No self_context/project_context/runtime_identity were set on this
    manager — those slot labels must NOT appear (unpopulated slots omitted)."""
    from cli.ui.commands.h_context import render_context_breakdown

    out = render_context_breakdown(mm_with_history)
    assert "self_context" not in out.lower()
    assert "project_context" not in out.lower()
    assert "runtime identity" not in out.lower()


def test_percent_falls_back_to_total_when_limit_unknown():
    """A stub manager with no max_input_tokens (or 0) still renders sane
    percentages — against the observed total, not a division by zero."""
    from cli.ui.commands.h_context import render_context_breakdown

    class _Stub:
        _system_message_tokens = 100
        _skill_message_tokens = 50
        max_input_tokens = 0

        class history:
            total_tokens = 50

    out = render_context_breakdown(_Stub())
    assert "context limit: unknown" in out.lower()
    # total = 200; system is 100/200 = 50.0%
    assert "50.0%" in out


# ---------------------------------------------------------------------------
# No session / graceful degradation
# ---------------------------------------------------------------------------


def test_none_manager_is_friendly_no_session_line():
    from cli.ui.commands.h_context import render_context_breakdown

    out = render_context_breakdown(None)
    assert "no active session" in out.lower()
    assert "/context" in out


def test_manager_missing_accessors_never_crashes():
    """A bare object lacking every ``_*_tokens``/``history``/``max_input_tokens``
    attribute must degrade gracefully (all slots omitted), never raise."""
    from cli.ui.commands.h_context import render_context_breakdown

    class _Empty:
        pass

    out = render_context_breakdown(_Empty())
    assert "no foundation slots populated yet" in out.lower()
    assert "total" in out.lower()


def test_history_without_total_tokens_attr_is_ignored():
    from cli.ui.commands.h_context import render_context_breakdown

    class _Stub:
        _system_message_tokens = 10
        history = object()  # no .total_tokens

    out = render_context_breakdown(_Stub())
    assert "system" in out.lower()
    assert "history" not in out.lower()


# ---------------------------------------------------------------------------
# Registered /context handler
# ---------------------------------------------------------------------------


def test_context_is_registered():
    from cli.ui.commands.handlers import build_default_registry

    reg = build_default_registry()
    assert reg.lookup("context") is not None


def test_handler_no_session_emits_friendly_line():
    """No conversation/agent on the CommandContext -> ctx.message_manager is
    None -> the handler emits the same friendly no-session line."""
    from cli.ui.commands.handlers import _h_context
    from cli.ui.commands.registry import CommandContext

    emitted = {}

    class _Ctx(CommandContext):
        def emit(self, text: str, *, title: str = "", style: str = "") -> None:
            emitted["text"] = text
            emitted["title"] = title

    ctx = _Ctx()
    _h_context(ctx)
    assert "no active session" in emitted["text"].lower()
    assert emitted["title"] == "context"


def test_handler_with_live_message_manager(mm_with_history):
    """ctx.message_manager resolves through ctx.agent.message_manager (the
    same seam /clear and /compact use); the handler renders the real manager."""
    import types

    from cli.ui.commands.handlers import _h_context
    from cli.ui.commands.registry import CommandContext

    emitted = {}

    class _Ctx(CommandContext):
        def emit(self, text: str, *, title: str = "", style: str = "") -> None:
            emitted["text"] = text

    conversation = types.SimpleNamespace(
        agent=types.SimpleNamespace(message_manager=mm_with_history)
    )
    ctx = _Ctx(conversation=conversation)
    _h_context(ctx)
    out = emitted["text"].lower()
    assert "system" in out and "skills" in out and "history" in out and "%" in out


# ---------------------------------------------------------------------------
# P5 (context-usage audit 2026-08-15): completeness rows — environment,
# tool catalog, tool schemas, and the last-known H-MEM count. All pure reads.
# ---------------------------------------------------------------------------


def test_render_includes_environment_and_tool_catalog(mm_with_history):
    from cli.ui.commands.h_context import render_context_breakdown
    mm_with_history.set_environment_message("<environment>local repl</environment>")
    mm_with_history.set_tool_catalog_message("<tool-catalog>rows</tool-catalog>")
    out = render_context_breakdown(mm_with_history).lower()
    assert "environment" in out
    assert "tool catalog" in out


def test_render_includes_tool_schema_row(mm_with_history):
    from cli.ui.commands.h_context import render_context_breakdown
    mm_with_history.set_tool_schema_tokens(4321)
    out = render_context_breakdown(mm_with_history)
    assert "tool schemas" in out.lower()
    assert "4,321" in out


def test_render_includes_last_known_hmem(mm_with_history):
    from cli.ui.commands.h_context import render_context_breakdown
    # The memo the real-count path (P7) leaves behind; a pure attribute read.
    mm_with_history._hmem_token_memo = ((10, 123), 987)
    out = render_context_breakdown(mm_with_history)
    assert "session memory" in out.lower()
    assert "987" in out


# ---------------------------------------------------------------------------
# F18/F19: provenance, the provider line, ephemerals, the schema flag
# ---------------------------------------------------------------------------


def test_estimates_are_marked_with_approximately(mm_with_history):
    from cli.ui.commands.h_context import render_context_breakdown
    out = render_context_breakdown(mm_with_history)
    assert "≈" in out, "a local estimate must never be printed as a measurement"
    assert "local estimate" in out


def test_no_provider_call_yet_says_so(mm_with_history):
    from cli.ui.commands.h_context import render_context_breakdown
    out = render_context_breakdown(mm_with_history)
    assert "last request (provider-reported): none yet this session" in out


def test_provider_line_appears_after_a_call(mm_with_history):
    from cli.ui.commands.h_context import render_context_breakdown

    mm_with_history.get_messages_for_llm(consume_ephemeral=False)
    mm_with_history.note_call_usage(output_tokens=120, input_tokens=9_000,
                                    cached_tokens=7_000, cache_creation_tokens=400)
    out = render_context_breakdown(mm_with_history)

    assert "last request (provider-reported): 9,000 prompt · 7,000 cached" in out
    assert "400 written" in out
    assert "2,000 uncached" in out
    # The total is MEASURED now, so the estimate mark is gone.
    assert "≈" not in out


def test_ephemerals_get_their_own_row(mm_with_history):
    from cli.ui.commands.h_context import render_context_breakdown
    from modules.llm.messages import HumanMessage

    mm_with_history.push_ephemeral_message(
        HumanMessage(content="a one-shot correspondent reply " * 20))
    out = render_context_breakdown(mm_with_history)
    assert "ephemerals" in out


def test_schema_slot_is_omitted_when_the_gauge_suppresses_it(mm_with_history, monkeypatch):
    from cli.ui.commands.h_context import render_context_breakdown

    mm_with_history.set_tool_schema_tokens(7_512)
    monkeypatch.delenv("CTX_COUNT_TOOL_SCHEMAS", raising=False)
    assert "tool schemas" in render_context_breakdown(mm_with_history)

    monkeypatch.setenv("CTX_COUNT_TOOL_SCHEMAS", "false")
    assert "tool schemas" not in render_context_breakdown(mm_with_history), (
        "/context must not render a slot the gauge suppresses"
    )
