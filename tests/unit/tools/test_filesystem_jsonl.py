"""056 WS2 — JSONL verbs shaped like Rob's data, and dict→string coercion.

Prod 2026-09-18/19: Rob edits `targets.jsonl` (642 rows) and `contact-log.jsonl`.
The tools offered `append_file` (string) and `coding_str_replace` (string), so a
JSON OBJECT passed as `old_string` was rejected 31 times, thinking loops followed,
and one pretty-printed 17-line record corrupted the store (22:51Z). A row is one
compact line; the tool should say so and do it. `jsonl_append` / `jsonl_remove` /
`jsonl_validate` are byte operations with the same confinement as every other
filesystem verb; `str_replace` and `append_file` coerce an object to its compact
line with a note instead of a validation error.
"""
import json
import logging
import os

import pytest

from core.exceptions import ServiceError


def _fs_tool():
    from tools.filesystem import FileSystem
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-jsonl")
    t.session_id = "s1"
    t.user_id = "u1"
    t._current_session_id = None
    t._enabled = True
    t._initialized = True
    return t


@pytest.fixture
def _pm(tmp_path, monkeypatch):
    from agents.task.path import PathManager, set_path_manager
    monkeypatch.setenv("FS_REALPATH_CONFINE", "on")
    pm = PathManager(data_root=str(tmp_path / "data"))
    set_path_manager(pm)
    return pm


def _ws(_pm):
    ws = _pm.get_workspace_dir("s1", "u1")
    os.makedirs(ws, exist_ok=True)
    return str(ws)


def _rows(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


@pytest.mark.asyncio
async def test_jsonl_append_writes_one_compact_line_per_object(_pm):
    from tools.controller.views import JsonlAppendAction
    ws = _ws(_pm)
    tool = _fs_tool()
    rec = {"handle": "RobinhoodVolume", "id": "2076186197454454784", "score": 45,
           "notes": "SPAM-WALL FLAG: bio is a 'Free $20' shortlink"}
    out = await tool.jsonl_append(JsonlAppendAction(file_path="data/x-targets/targets.jsonl",
                                                    record=rec))
    assert "1 record" in out and "1 line" in out
    out = await tool.jsonl_append(JsonlAppendAction(file_path="data/x-targets/targets.jsonl",
                                                    record=[{"id": "1"}, {"id": "2"}]))
    assert "2 record" in out and "3 line" in out
    p = os.path.join(ws, "data/x-targets/targets.jsonl")
    with open(p, encoding="utf-8") as f:
        lines = f.read().split("\n")
    assert lines[0] == json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    assert lines[-1] == "" and len([l for l in lines if l]) == 3


@pytest.mark.asyncio
async def test_jsonl_append_refuses_non_objects(_pm):
    from tools.controller.views import JsonlAppendAction
    _ws(_pm)
    tool = _fs_tool()
    with pytest.raises(ServiceError):
        await tool.jsonl_append(JsonlAppendAction(file_path="x.jsonl", record=["not", "objects"]))


@pytest.mark.asyncio
async def test_jsonl_remove_backs_up_verifies_and_reports(_pm):
    from tools.controller.views import JsonlRemoveAction
    ws = _ws(_pm)
    p = os.path.join(ws, "store.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for i in range(20):
            f.write(json.dumps({"id": str(i), "handle": f"h{i}"}) + "\n")
    tool = _fs_tool()
    out = await tool.jsonl_remove(JsonlRemoveAction(file_path="store.jsonl", key="id",
                                                    values=["3", "17", "99"]))
    assert "removed 2" in out and "18" in out and "not found: 99" in out
    assert [r["id"] for r in _rows(p)] == [str(i) for i in range(20) if i not in (3, 17)]
    assert os.path.exists(p + ".bak"), "a rewrite always leaves the pre-edit copy"
    assert len(_rows(p + ".bak")) == 20


@pytest.mark.asyncio
async def test_jsonl_remove_refuses_on_a_bad_line_and_leaves_the_store_untouched(_pm):
    from tools.controller.views import JsonlRemoveAction
    ws = _ws(_pm)
    p = os.path.join(ws, "store.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"id":"1"}\n{\n  "id": "2"\n}\n')  # the 22:51Z corruption shape
    before = open(p, encoding="utf-8").read()
    tool = _fs_tool()
    with pytest.raises(ServiceError):
        await tool.jsonl_remove(JsonlRemoveAction(file_path="store.jsonl", key="id", values=["1"]))
    assert open(p, encoding="utf-8").read() == before


@pytest.mark.asyncio
async def test_jsonl_validate_counts_and_names_bad_lines_and_duplicates(_pm):
    from tools.controller.views import JsonlValidateAction
    ws = _ws(_pm)
    p = os.path.join(ws, "store.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"id":"1"}\n{"id":"2"}\nnot json\n{"id":"1"}\n')
    tool = _fs_tool()
    out = await tool.jsonl_validate(JsonlValidateAction(file_path="store.jsonl", key="id"))
    assert "4 lines" in out and "3 valid" in out and "bad line 3" in out
    assert "duplicate id" in out and "'1'" in out


def test_str_replace_params_coerce_an_object_to_its_compact_line():
    from tools.coding.tool import StrReplaceParams
    rec = {"handle": "Alahlyloo", "score": 55}
    p = StrReplaceParams(file_path="data/x-targets/targets.jsonl", old_string=rec,
                         new_string={"handle": "Alahlyloo", "score": 60})
    assert p.old_string == json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    assert p.new_string.startswith('{"handle":"Alahlyloo"')
    assert p.coerced_note and "coerced" in p.coerced_note


def test_append_file_action_coerces_an_object_to_its_compact_line():
    from tools.controller.views import AppendFileAction
    a = AppendFileAction(file_path="c.jsonl", content={"id": "1", "x": "é"})
    assert a.content == '{"id":"1","x":"é"}\n'
