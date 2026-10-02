"""F13 — the foundation survives a restart, so the prompt prefix does too.

``message_history.json`` restored the CONVERSATION byte-for-byte and re-rendered
every FOUNDATION block from the live environment: the system prompt, the
runtime-identity line, ``<environment>`` (which embeds the live tool set and
budget), self/project context, the skill and tool catalogs, and the emitted tool
ORDER. A deploy, a flag flip, a tool that failed to construct or an MCP server
that did not come back therefore re-wrote the whole cached prefix.

These tests pin the three answers: replay when model/provider/cwd match, ONE
warning and a fresh render when they do not, and a fresh render — never an empty
prompt — for a corrupt blob or a flag that is off.
"""

import json

import pytest
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.path import get_path_manager, set_path_manager, pm
from tests.support.prefix_cache import serialize_messages

SESSION = "f13-foundation-session"
USER = "u_f13"


def _mm(session_id=SESSION, model="gpt-4o"):
    llm = MagicMock()
    llm.model_name = model
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=8000,
        session_id=session_id,
    )


def _dress(mm, suffix=""):
    """Fill every foundation slot with recognisable rendered bytes."""
    mm.set_runtime_identity(f"model{suffix}", f"provider{suffix}")
    mm.set_environment_message(f"the environment as rendered{suffix}")
    mm.set_self_context_message(f"who I am{suffix}")
    mm.set_project_context_message(f"the project rules{suffix}")
    mm.set_skill_message(f"skill catalog{suffix}")
    mm.set_tool_catalog_message(f"- done: finish [ready]{suffix}")
    return mm


def _history_file(session_id=SESSION, user_id=USER):
    return pm().create_file_path(
        session_id=session_id, subdir_name="memory",
        filename="message_history.json", user_id=user_id)


@pytest.fixture()
def tmp_data_root(tmp_path):
    set_path_manager(get_path_manager(data_root=str(tmp_path)))
    yield tmp_path


# ---------------------------------------------------------------------------
# The happy path: the replayed request is byte-identical.
# ---------------------------------------------------------------------------


def test_the_reloaded_request_is_byte_identical(tmp_data_root):
    before = _dress(_mm())
    before.add_human_message("what is the plan")
    before._foundation_tool_names = ["done", "read_file", "web_search"]
    expected = serialize_messages(before.get_messages_for_llm(consume_ephemeral=False))
    before.save_to_disk(SESSION, USER)

    # A NEW manager renders a DIFFERENT foundation from the live environment —
    # exactly what a deploy or a flag flip does.
    after = _dress(_mm(), suffix=" (rebuilt after restart)")
    assert serialize_messages(
        after.get_messages_for_llm(consume_ephemeral=False)) != expected

    assert after.load_from_disk(SESSION, USER) is True
    assert serialize_messages(
        after.get_messages_for_llm(consume_ephemeral=False)) == expected
    assert after._foundation_tool_names == ["done", "read_file", "web_search"]


def test_the_foundation_object_lands_in_the_one_file(tmp_data_root):
    mm = _dress(_mm())
    mm._foundation_tool_names = ["done"]
    mm.save_to_disk(SESSION, USER)

    blob = json.loads(_history_file().read_text())["foundation"]
    for key in ("model", "provider", "cwd", "system_prompt", "runtime_identity",
                "environment", "self_context", "project_context", "skill_catalog",
                "tool_catalog", "initial_task", "tool_names"):
        assert key in blob, key
    assert blob["tool_names"] == ["done"]
    assert "the environment as rendered" in blob["environment"]


# ---------------------------------------------------------------------------
# The three rebuild triggers, the corrupt blob and the flag.
# ---------------------------------------------------------------------------


def test_a_changed_model_warns_and_keeps_the_fresh_render(tmp_data_root, caplog):
    _dress(_mm()).save_to_disk(SESSION, USER)

    after = _dress(_mm(model="claude-opus-4"), suffix=" FRESH")
    fresh = serialize_messages(after.get_messages_for_llm(consume_ephemeral=False))
    with caplog.at_level("WARNING"):
        after.load_from_disk(SESSION, USER)
    assert serialize_messages(
        after.get_messages_for_llm(consume_ephemeral=False)) == fresh
    warnings = [r for r in caplog.records
                if "foundation rebuilt (model/provider/cwd changed)" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


def test_a_changed_cwd_keeps_the_fresh_render(tmp_data_root, monkeypatch):
    _dress(_mm()).save_to_disk(SESSION, USER)
    monkeypatch.setattr("os.getcwd", lambda: "/somewhere/else/entirely")

    after = _dress(_mm(), suffix=" FRESH")
    fresh = serialize_messages(after.get_messages_for_llm(consume_ephemeral=False))
    after.load_from_disk(SESSION, USER)
    assert serialize_messages(
        after.get_messages_for_llm(consume_ephemeral=False)) == fresh


@pytest.mark.parametrize("corrupt", [
    {"model": "gpt-4o"},                                   # half written
    {"model": 1, "provider": "p", "cwd": "/x"},            # wrong types
    "not even an object",
    {"model": "gpt-4o", "provider": "p", "cwd": "/x", "system_prompt": ""},
])
def test_a_corrupt_foundation_falls_back_to_the_fresh_render(tmp_data_root, corrupt):
    _dress(_mm()).save_to_disk(SESSION, USER)
    path = _history_file()
    data = json.loads(path.read_text())
    data["foundation"] = corrupt
    path.write_text(json.dumps(data))

    after = _dress(_mm(), suffix=" FRESH")
    fresh = serialize_messages(after.get_messages_for_llm(consume_ephemeral=False))
    assert after.load_from_disk(SESSION, USER) is True
    reloaded = after.get_messages_for_llm(consume_ephemeral=False)
    assert serialize_messages(reloaded) == fresh
    assert reloaded and str(reloaded[0].content).strip(), "never an empty prompt"


def test_a_missing_foundation_key_is_the_pre_f13_rebuild(tmp_data_root):
    _dress(_mm()).save_to_disk(SESSION, USER)
    path = _history_file()
    data = json.loads(path.read_text())
    data.pop("foundation")
    path.write_text(json.dumps(data))

    after = _dress(_mm(), suffix=" FRESH")
    fresh = serialize_messages(after.get_messages_for_llm(consume_ephemeral=False))
    after.load_from_disk(SESSION, USER)
    assert serialize_messages(
        after.get_messages_for_llm(consume_ephemeral=False)) == fresh


@pytest.mark.parametrize("missing", [
    "runtime_identity", "environment", "self_context", "project_context",
    "initial_task", "skill_catalog", "tool_catalog", "tool_names",
])
def test_partial_foundation_never_mixes_saved_and_live_slots(tmp_data_root, missing):
    _dress(_mm()).save_to_disk(SESSION, USER)
    path = _history_file()
    data = json.loads(path.read_text())
    del data["foundation"][missing]
    path.write_text(json.dumps(data))

    after = _dress(_mm(), suffix=" FRESH")
    fresh = serialize_messages(after.get_messages_for_llm(consume_ephemeral=False))
    assert after.load_from_disk(SESSION, USER)
    assert serialize_messages(after.get_messages_for_llm(consume_ephemeral=False)) == fresh


def test_flag_off_rebuilds_always(tmp_data_root, monkeypatch):
    _dress(_mm()).save_to_disk(SESSION, USER)
    monkeypatch.setenv("FOUNDATION_REPLAY", "false")

    after = _dress(_mm(), suffix=" FRESH")
    fresh = serialize_messages(after.get_messages_for_llm(consume_ephemeral=False))
    after.load_from_disk(SESSION, USER)
    assert serialize_messages(
        after.get_messages_for_llm(consume_ephemeral=False)) == fresh


# ---------------------------------------------------------------------------
# The tool emit order.
# ---------------------------------------------------------------------------


def test_emitted_tool_names_reads_both_wire_shapes():
    from agents.task.agent.messages.foundation_replay import emitted_tool_names
    assert emitted_tool_names([
        {"type": "function", "function": {"name": "done"}},
        {"name": "read_file"},
        "junk",
        {"function": {}},
    ]) == ["done", "read_file"]


def _registry_with(names):
    from tools.controller.registry.service import Registry
    registry = Registry()
    for name in names:
        async def _fn(text: str = ""):
            return text
        _fn.__name__ = name
        registry.action(description=f"the {name} action")(_fn)
    return registry


def test_preferred_order_puts_known_names_first():
    from agents.task.agent.messages.foundation_replay import emitted_tool_names
    registry = _registry_with(["alpha", "bravo", "charlie", "delta"])

    plain = emitted_tool_names(registry.get_all_actions_for_provider("openai"))
    assert plain.index("alpha") < plain.index("bravo") < plain.index("charlie")

    preferred = emitted_tool_names(registry.get_all_actions_for_provider(
        "openai", preferred_order=["delta", "charlie"]))
    assert preferred[:2] == ["delta", "charlie"]
    # everything the order does not name keeps the F1 sorted order and follows
    rest = [n for n in preferred[2:] if n in ("alpha", "bravo")]
    assert rest == ["alpha", "bravo"]
    assert sorted(preferred) == sorted(plain), "no action may be dropped"


def test_the_memo_key_distinguishes_two_orders():
    from agents.task.agent.messages.foundation_replay import emitted_tool_names
    registry = _registry_with(["alpha", "bravo", "charlie"])

    first = emitted_tool_names(registry.get_all_actions_for_provider(
        "openai", preferred_order=["charlie"]))
    second = emitted_tool_names(registry.get_all_actions_for_provider(
        "openai", preferred_order=["bravo"]))
    assert first[0] == "charlie" and second[0] == "bravo", (first, second)


def test_an_unchanged_catalog_refresh_does_not_undo_the_replay(tmp_data_root):
    """The replayed catalog must survive the next `set_tool_catalog_message`.

    `_tool_catalog_source` holds the RAW text the setter was handed; the persisted
    slot holds the ENVELOPED message content. Writing the envelope into the source
    would make the very next refresh read as a change and rewrite the block the
    replay just restored.
    """
    before = _dress(_mm())
    before.save_to_disk(SESSION, USER)

    after = _dress(_mm(), suffix=" FRESH")
    assert after.load_from_disk(SESSION, USER) is True
    replayed = after._tool_catalog_message.content

    # The live environment renders the SAME catalog it rendered at construction.
    after.set_tool_catalog_message("- done: finish [ready] FRESH")
    assert after._tool_catalog_message.content == replayed
