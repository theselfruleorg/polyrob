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


def _dress(mm, suffix="", rules_suffix=""):
    """Fill every foundation slot with recognisable rendered bytes.

    2026-10-13 A2: ``suffix`` varies the layers that REPLAY (identity, project
    context, tool catalog); ``rules_suffix`` varies the ``refresh_on_reload``
    layers (identity since 095 W1a, environment, self-context, skills), whose
    differing fresh render stands on reload."""
    mm.set_runtime_identity(f"model{rules_suffix}", f"provider{rules_suffix}")
    mm.set_environment_message(f"the environment as rendered{rules_suffix}")
    mm.set_self_context_message(f"who I am{rules_suffix}")
    mm.set_project_context_message(f"the project rules{suffix}")
    mm.set_skill_message(f"skill catalog{rules_suffix}")
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


def test_the_replay_fields_survive_the_session_file(tmp_data_root):
    from modules.llm.messages import AIMessage
    before = _dress(_mm())
    message = AIMessage(content="a")
    blocks = [{"block": {"type": "thinking", "signature": "s"}}]
    message.additional_kwargs.update(reasoning_content="why", reasoning_seat="deepseek-native",
                                     anthropic_thinking=blocks)
    before._add_message_with_tokens(message, _internal=True)
    before.save_to_disk(SESSION, USER)

    after = _mm()
    assert after.load_from_disk(SESSION, USER) is True
    kept = after.history.messages[-1].message.additional_kwargs
    assert (kept["reasoning_content"], kept["reasoning_seat"], kept["anthropic_thinking"]) == ("why", "deepseek-native", blocks)


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


# ---------------------------------------------------------------------------
# The tool emit order.
# ---------------------------------------------------------------------------


def test_schema_tool_names_reads_both_wire_shapes():
    from modules.llm.tool_schemas import schema_tool_names
    assert schema_tool_names([
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
    from modules.llm.tool_schemas import schema_tool_names
    registry = _registry_with(["alpha", "bravo", "charlie", "delta"])

    plain = schema_tool_names(registry.get_all_actions_for_provider("openai"))
    assert plain.index("alpha") < plain.index("bravo") < plain.index("charlie")

    preferred = schema_tool_names(registry.get_all_actions_for_provider(
        "openai", preferred_order=["delta", "charlie"]))
    assert preferred[:2] == ["delta", "charlie"]
    # everything the order does not name keeps the F1 sorted order and follows
    rest = [n for n in preferred[2:] if n in ("alpha", "bravo")]
    assert rest == ["alpha", "bravo"]
    assert sorted(preferred) == sorted(plain), "no action may be dropped"


def test_the_memo_key_distinguishes_two_orders():
    from modules.llm.tool_schemas import schema_tool_names
    registry = _registry_with(["alpha", "bravo", "charlie"])

    first = schema_tool_names(registry.get_all_actions_for_provider(
        "openai", preferred_order=["charlie"]))
    second = schema_tool_names(registry.get_all_actions_for_provider(
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


def test_a_deploy_keeps_the_prefix_and_the_tail_states_the_running_version(tmp_data_root, monkeypatch):
    """095 W1a (SK-2/SK-9) with 106 C4: a resumed chat states the version that RUNS, and
    a deploy no longer colds its foundation to say so."""
    import core.version as v
    monkeypatch.setattr(v, "running_version_line", lambda: "v1.0.0 (deployed aaaaaaaaaaaa)")
    before = _dress(_mm())
    expected = serialize_messages(before.get_messages_for_llm(consume_ephemeral=False))
    before.save_to_disk(SESSION, USER)
    assert "version" not in json.loads(_history_file().read_text())["foundation"]

    monkeypatch.setattr(v, "running_version_line", lambda: "v1.0.1 (deployed bbbbbbbbbbbb)")
    after = _dress(_mm())
    assert after.load_from_disk(SESSION, USER)
    assert serialize_messages(after.get_messages_for_llm(consume_ephemeral=False))[:len(expected) - 1] \
        == expected[:-1]
    after.offer_running_version()
    assert after.update_state_delta({})
    tail = str(after.history.messages[-1].message.content)
    assert "<running-version>\nRunning version: v1.0.1 (deployed bbbbbbbbbbbb).\n</running-version>" in tail


def test_an_old_blob_with_a_version_key_replays(tmp_data_root):
    from agents.task.agent.messages.foundation_replay import replay_foundation, capture_foundation
    mm = _dress(_mm())
    blob = capture_foundation(mm)
    blob["version"] = "v0.9.0 (deployed old)"
    assert replay_foundation(mm, blob) is True


# ---------------------------------------------------------------------------
# 106 C6: a changed row keeps its bytes, and the change rides the tail
# ---------------------------------------------------------------------------


def _reloaded_with(tmp_data_root, **fresh):
    before = _mm()
    before.set_environment_message("Host: a\nautonomy: ENABLED")
    before.set_self_context_blocks((("owner_rules", "rule one\nrule two"),))
    before.set_skill_message("- skill-a\n- skill-b")
    before.set_tool_catalog_message("- done [ready]")
    before.save_to_disk(SESSION, USER)
    saved = serialize_messages(before.get_messages_for_llm(consume_ephemeral=False))

    after = _mm()
    after.set_environment_message(fresh.get("environment", "Host: a\nautonomy: ENABLED"))
    after.set_self_context_blocks((("owner_rules", fresh.get("rules", "rule one\nrule two")),))
    after.set_skill_message(fresh.get("skills", "- skill-a\n- skill-b"))
    after.set_tool_catalog_message("- done [ready]")
    assert after.load_from_disk(SESSION, USER)
    return saved, after


def _tail_text(mm):
    mm.update_state_delta({})
    return "\n".join(str(m.message.content) for m in mm.history.messages)


def test_a_changed_row_keeps_its_bytes_and_the_tail_says_what_changed(tmp_data_root):
    saved, after = _reloaded_with(tmp_data_root, environment="Host: a\nautonomy: PAUSED",
                                  rules="rule one\nrule three", skills="- skill-a\n- skill-c")
    assert serialize_messages(after.get_messages_for_llm(consume_ephemeral=False)) == saved
    tail = _tail_text(after)
    assert "autonomy: PAUSED" in tail and "autonomy: ENABLED" in tail
    assert "rule three" in tail and "rule two" in tail and "rule one" not in tail
    assert "skill-c" in tail
    assert "This overrides it." in tail


def test_no_change_means_no_tail_part(tmp_data_root):
    _saved, after = _reloaded_with(tmp_data_root)
    assert after.update_state_delta({}) is False


def test_a_second_reload_keeps_the_same_bytes_and_says_it_again(tmp_data_root):
    saved, after = _reloaded_with(tmp_data_root, rules="rule one\nrule three")
    after.save_to_disk(SESSION, USER)
    again = _mm()
    again.set_self_context_blocks((("owner_rules", "rule one\nrule three"),))
    assert again.load_from_disk(SESSION, USER)
    assert any("rule three" in part for part in again._standing_state_parts.values())


def test_a_change_too_long_for_one_part_rewrites_the_row(tmp_data_root):
    """A cut must not hide a rule: over the part cap the fresh render stands."""
    long_rules = "\n".join(f"rule number {i}" for i in range(600))
    _saved, after = _reloaded_with(tmp_data_root, rules=long_rules)
    assert any("rule number 599" in str(m.content) for m in after._self_context_blocks)
    assert "self_context_update" not in after._standing_state_parts


def test_a_system_prompt_change_stands_at_once(tmp_data_root):
    """A rule of the system prompt cannot be replaced from a user-role row."""
    from agents.task.agent.messages.foundation_replay import capture_foundation, replay_foundation
    mm = _dress(_mm())
    blob = capture_foundation(mm)
    blob["system_prompt"] = "an older system prompt"
    fresh = mm._system_message.content
    assert replay_foundation(mm, blob) is True
    assert mm._system_message.content == fresh


def test_a_tainted_seat_loses_the_owner_part_of_the_delta(tmp_data_root):
    """SK-18: a seat tainted after the reload must not keep an owner rule in its tail."""
    _saved, after = _reloaded_with(tmp_data_root, rules="rule one\nprivate rule three")
    assert after.update_state_delta({})
    assert "private rule three" in _tail_text(after)
    after._owner_context_allowed = lambda: False
    after.update_state_delta({})
    assert "private rule three" not in "\n".join(
        str(m.message.content) for m in after.history.messages)
