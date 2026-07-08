"""T1-03 (2026-07-06 structural review): in native-tools mode the system prompt's
<available-actions> section duplicated the ENTIRE tool schema (per-action raw
Python-repr of the JSON-schema properties) while the same schemas ship to the
provider in the `tools` param — 2.5–6k redundant tokens per session.

Fix: a compact one-line-per-action index (name + first line of description) for
native mode; the full dump remains for the JSON-fallback path, where it is the
only schema the model sees.
"""
import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from pydantic import BaseModel

from tools.controller.registry.views import ActionRegistry, RegisteredAction


class _Params(BaseModel):
    text: str = "x"
    count: int = 1


def _action(name, description, tool=None):
    return RegisteredAction(
        name=name, description=description, function=lambda: None,
        param_model=_Params, tool=tool,
    )


def _registry():
    reg = ActionRegistry()
    reg.actions["done"] = _action("done", "Complete task - use when finished")
    reg.actions["fetch_url"] = _action(
        "fetch_url",
        "Fetch a URL and return markdown.\nSecond line with internal detail.",
        tool="web_fetch",
    )
    return reg


def test_index_is_one_line_per_action_without_schema_dump():
    idx = _registry().get_prompt_action_index()
    assert "- done: Complete task - use when finished" in idx
    # no raw schema-properties repr (the T1-03 duplication artifact)
    assert "'type'" not in idx
    assert "model_json_schema" not in idx
    assert "{done:" not in idx


def test_index_uses_first_description_line_only():
    idx = _registry().get_prompt_action_index()
    assert "- fetch_url: Fetch a URL and return markdown." in idx
    assert "Second line with internal detail" not in idx


def test_index_groups_by_tool():
    idx = _registry().get_prompt_action_index()
    assert "General Actions:" in idx
    assert "Web_fetch Tool Actions:" in idx


def test_controller_exposes_compact_index(tmp_path):
    import types

    from tools.controller.service import Controller

    orch = types.SimpleNamespace(
        session_id="s1", user_id="u1", workspace_dir=str(tmp_path)
    )
    c = Controller(orchestrator=orch)
    idx = c.get_prompt_action_index()
    assert "- done:" in idx
    assert "- send_message:" in idx
    # the compact index must be dramatically smaller than the schema dump
    assert len(idx) < len(c.get_prompt_description()) / 2


def test_construction_uses_index_in_native_mode():
    # The wiring point: native mode uses the compact index; fallback mode keeps
    # the full dump (there it is the only schema the model ever sees).
    import inspect
    from agents.task.agent.core import construction

    src = inspect.getsource(construction)
    assert "get_prompt_action_index()" in src
