"""A file BUILT via the coding tool gets an artifact row too, not only the
filesystem tool's writes.

The ledger is the reliable source the `artifact` acceptance check + retry
continuity resolve through. The coding tool writes with bare open(), so before
this a report authored by create_file/str_replace/apply_patch was invisible to
the ledger and a real goal failed on "never produced" for evidence on disk.
"""
import logging

import pytest

from core.artifacts import get_artifact_ledger
from tools.coding.tool import (
    CodingTool, CreateFileParams, StrReplaceParams,
)


def _tool(root, *, user_id="rob", session_id="s1"):
    t = object.__new__(CodingTool)
    t.logger = logging.getLogger("coding-artifacts")
    t._root_override = str(root)
    t._backend = None
    t.user_id = user_id
    t.session_id = session_id
    return t


@pytest.mark.asyncio
async def test_create_file_records_an_artifact(tmp_path):
    res = await _tool(tmp_path).create_file(
        CreateFileParams(file_path="report.md", content="# findings\n"))
    assert res.error is None

    rows = get_artifact_ledger().list_for_session("rob", "s1")
    assert len(rows) == 1
    assert rows[0].path.endswith("report.md")
    assert rows[0].kind == "report"
    assert get_artifact_ledger().verify(rows[0].id, "rob") == "ok"


@pytest.mark.asyncio
async def test_str_replace_records_the_edited_file(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    t = _tool(tmp_path)
    res = await t.str_replace(StrReplaceParams(
        file_path="app.py", old_string="x = 1", new_string="x = 2"))
    assert res.error is None

    rows = get_artifact_ledger().list_for_session("rob", "s1")
    assert len(rows) == 1
    assert rows[0].path.endswith("app.py")
    assert rows[0].kind == "code"


@pytest.mark.asyncio
async def test_no_user_id_records_nothing(tmp_path):
    # A tool with no resolvable tenant must not write an anonymous row.
    res = await _tool(tmp_path, user_id=None).create_file(
        CreateFileParams(file_path="x.md", content="y"))
    assert res.error is None
    assert get_artifact_ledger().list_for_session("rob", "s1") == []


# --------------------------------------------------------------------------- #
# 043 A18: record_artifact() now returns the row id, and create_file /
# str_replace / apply_patch stamp it onto ActionResult.metadata["artifact_id"]
# so a caller can read it straight off the result instead of a second lookup.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_create_file_stamps_artifact_id_on_the_result(tmp_path):
    res = await _tool(tmp_path).create_file(
        CreateFileParams(file_path="report.md", content="# findings\n"))
    assert res.error is None

    row = get_artifact_ledger().list_for_session("rob", "s1")[0]
    assert res.metadata == {"artifact_id": row.id}


@pytest.mark.asyncio
async def test_str_replace_stamps_artifact_id_on_the_result(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    t = _tool(tmp_path)
    res = await t.str_replace(StrReplaceParams(
        file_path="app.py", old_string="x = 1", new_string="x = 2"))
    assert res.error is None

    row = get_artifact_ledger().list_for_session("rob", "s1")[0]
    assert res.metadata == {"artifact_id": row.id}


@pytest.mark.asyncio
async def test_apply_patch_stamps_artifact_id_on_the_result(tmp_path):
    from tools.coding.tool import ApplyPatchParams

    (tmp_path / "app.py").write_text("x = 1\n")
    t = _tool(tmp_path)
    patch = (
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    )
    res = await t.apply_patch(ApplyPatchParams(file_path="app.py", patch=patch))
    assert res.error is None

    row = get_artifact_ledger().list_for_session("rob", "s1")[0]
    assert res.metadata == {"artifact_id": row.id}


@pytest.mark.asyncio
async def test_no_user_id_leaves_metadata_unset(tmp_path):
    res = await _tool(tmp_path, user_id=None).create_file(
        CreateFileParams(file_path="x.md", content="y"))
    assert res.error is None
    assert res.metadata is None
