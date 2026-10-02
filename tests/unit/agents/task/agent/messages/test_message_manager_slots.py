"""F29 — ``MessageManager.__slots__`` must actually hold.

The class declares a 40-odd-name ``__slots__`` and four of its modules carry a
comment claiming "no ``__dict__``". Neither was true: twelve attributes were
assigned across the mixins and declared nowhere, and three mixins
(``GuidanceMixin``, ``MessageBuildersMixin``, ``MessageRetrievalMixin``) omitted
``__slots__`` entirely — one class in the MRO without it gives every instance a
``__dict__``, so the tuple saved nothing and a typo'd attribute name was a
silent no-op instead of an error.

These tests are the guard: a new undeclared attribute now fails HERE rather than
quietly re-opening the dict.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt


@pytest.fixture
def mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=8000,
        session_id="s-slots",
    )


def test_instances_have_no_dict(mm: MessageManager):
    assert not hasattr(mm, "__dict__"), (
        "a class in the MRO is missing __slots__, so every MessageManager "
        "carries a __dict__ and the declared slots save nothing"
    )


def test_an_undeclared_attribute_raises(mm: MessageManager):
    with pytest.raises(AttributeError):
        mm.bogus = 1


def test_every_class_in_the_mro_declares_slots():
    missing = [
        klass.__name__
        for klass in MessageManager.__mro__
        if klass is not object and "__slots__" not in klass.__dict__
    ]
    assert not missing, (
        "these classes omit __slots__, which re-opens __dict__ for the whole "
        f"composition: {missing}"
    )


def test_the_attributes_the_mixins_assign_are_declared(mm: MessageManager):
    """The twelve names F29 found undeclared must stay declared."""
    declared: set[str] = set()
    for klass in MessageManager.__mro__:
        declared.update(getattr(klass, "__slots__", ()) or ())

    for name in (
        "_environment_message", "_environment_tokens", "_ephemeral_pending",
        "_history_budget_tokens", "_history_secret_scrub", "_hmem_token_memo",
        "_persona_block", "_surface_profile", "_tool_catalog_source",
        "_tool_schema_tokens", "_verbosity", "tool_ids",
        # F6 added these in the same wave.
        "_usage_anchor", "_last_call_usage", "_hmem_in_last_prompt",
    ):
        assert name in declared, f"{name} is assigned but not declared in __slots__"


def test_the_live_write_paths_still_work(mm: MessageManager):
    """Exercise the assignments that would now raise if a name were missed."""
    mm.set_environment_message("where the agent lives")
    mm.set_tool_catalog_message("<tool-catalog>a</tool-catalog>")
    mm.set_skill_message("a skill")
    mm.set_tool_schema_tokens(1234)
    mm.get_messages_for_llm(consume_ephemeral=True)
    mm.note_call_usage(output_tokens=1, input_tokens=2, cached_tokens=0)
    assert mm.context_usage()["slots"]["tool_schemas"] == 1234
