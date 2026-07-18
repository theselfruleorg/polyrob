"""C0 + tool-level tests: registration gating, str_replace/grep/run_tests, confinement."""
import logging
import os

import pytest

import agents.task.agent.service  # noqa: F401 — avoid controller import cycle
from tools.coding import register_coding_tool
from tools.coding.tool import (
    CodingTool, StrReplaceParams, GrepParams, RunTestsParams, ApplyPatchParams,
)


# --- C0 registration gating --------------------------------------------------

def test_flag_off_not_registered(monkeypatch):
    monkeypatch.delenv("CODING_TOOLS_ENABLED", raising=False)
    from tools.descriptors import TOOL_DESCRIPTORS, get_tool_class
    TOOL_DESCRIPTORS.pop("coding", None)
    assert register_coding_tool() is False
    assert get_tool_class("coding") is None


def test_flag_on_registers(monkeypatch):
    monkeypatch.setenv("CODING_TOOLS_ENABLED", "true")
    from tools.descriptors import TOOL_DESCRIPTORS, TOOL_COMPONENTS, get_tool_class
    try:
        assert register_coding_tool() is True
        assert get_tool_class("coding") is CodingTool
    finally:
        TOOL_DESCRIPTORS.pop("coding", None)
        TOOL_COMPONENTS[:] = [(n, c) for (n, c) in TOOL_COMPONENTS if n != "coding"]


def test_coding_not_in_default_tools():
    from tools.descriptors import get_default_tools
    assert "coding" not in get_default_tools()


def test_tool_module_has_no_future_annotations():
    """LANDMINE: the action-closure module must not stringize annotations.

    ``from __future__ import annotations`` binds the module name ``annotations``
    to the __future__ feature object — detect that precisely (not a docstring
    mention).
    """
    import __future__
    import tools.coding.tool as m
    assert getattr(m, "annotations", None) is not __future__.annotations


# --- helpers -----------------------------------------------------------------

def _tool(root):
    t = object.__new__(CodingTool)
    t.logger = logging.getLogger("coding-test")
    t._root_override = str(root)
    t._backend = None
    return t


# --- str_replace -------------------------------------------------------------

@pytest.mark.asyncio
async def test_str_replace_edits_file(tmp_path):
    (tmp_path / "x.py").write_text("a = 1\nb = 2\n")
    t = _tool(tmp_path)
    res = await t.str_replace(StrReplaceParams(file_path="x.py", old_string="a = 1", new_string="a = 99"))
    assert getattr(res, "error", None) in (None, "")
    assert (tmp_path / "x.py").read_text() == "a = 99\nb = 2\n"


@pytest.mark.asyncio
async def test_str_replace_ambiguous_fails_loud(tmp_path):
    (tmp_path / "x.py").write_text("z\nz\n")
    t = _tool(tmp_path)
    res = await t.str_replace(StrReplaceParams(file_path="x.py", old_string="z", new_string="q"))
    assert res.error and "unique" in res.error
    assert (tmp_path / "x.py").read_text() == "z\nz\n"  # unchanged


@pytest.mark.asyncio
async def test_str_replace_path_escape_blocked(tmp_path):
    t = _tool(tmp_path)
    res = await t.str_replace(StrReplaceParams(file_path="../../etc/passwd", old_string="x", new_string="y"))
    assert res.error and ("escape" in res.error.lower() or "outside" in res.error.lower())


# --- grep --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grep_finds_pattern(tmp_path):
    (tmp_path / "a.py").write_text("TARGET = 1\n")
    t = _tool(tmp_path)
    res = await t.grep(GrepParams(pattern="TARGET", output_mode="content"))
    assert getattr(res, "error", None) in (None, "")
    assert "TARGET" in res.extracted_content and "a.py" in res.extracted_content


# --- run_tests ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_tests_reports_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    t = _tool(tmp_path)
    res = await t.run_tests(RunTestsParams(command="python -c \"print('ok')\""))
    assert getattr(res, "error", None) in (None, "")
    assert "ok" in res.extracted_content


@pytest.mark.asyncio
async def test_run_tests_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    t = _tool(tmp_path)
    res = await t.run_tests(RunTestsParams(command="python -c \"import sys; sys.exit(1)\""))
    assert res.error


# --- apply_patch -------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_patch_action_edits_file(tmp_path):
    tool = _tool(tmp_path)
    f = tmp_path / "m.py"
    f.write_text("a\nb\nc\n")
    res = await tool.apply_patch(
        ApplyPatchParams(file_path="m.py", patch="@@ -2,1 +2,1 @@\n-b\n+B\n")
    )
    assert res.error is None
    assert f.read_text() == "a\nB\nc\n"


@pytest.mark.asyncio
async def test_apply_patch_action_rejects_mismatch(tmp_path):
    tool = _tool(tmp_path)
    (tmp_path / "m.py").write_text("a\nb\n")
    res = await tool.apply_patch(
        ApplyPatchParams(file_path="m.py", patch="@@ -1,1 +1,1 @@\n-Z\n+Y\n")
    )
    assert res.error is not None and "mismatch" in res.error


# --- I-2/H1 LSP diagnostics-after-edit wiring --------------------------------

@pytest.mark.asyncio
async def test_str_replace_appends_diagnostics_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_LSP_ENABLED", "true")
    monkeypatch.setattr(
        "tools.coding.lsp.diagnose_file",
        lambda path, root, timeout_sec=8.0, runner=None: "x.py:1:1 undefined name 'foo'",
    )
    (tmp_path / "x.py").write_text("a = 1\n")
    t = _tool(tmp_path)
    res = await t.str_replace(
        StrReplaceParams(file_path="x.py", old_string="a = 1", new_string="a = 2")
    )
    assert getattr(res, "error", None) in (None, "")
    assert "<diagnostics>" in res.extracted_content
    assert "undefined name 'foo'" in res.extracted_content
    assert res.extracted_content.startswith("Edited x.py (1 replacement).")


@pytest.mark.asyncio
async def test_str_replace_flag_off_byte_identical_no_subprocess(tmp_path, monkeypatch):
    monkeypatch.delenv("CODING_LSP_ENABLED", raising=False)
    calls = []

    def _spy_runner(cmd, cwd, timeout_sec):
        calls.append(cmd)
        raise AssertionError("runner must not be invoked when CODING_LSP_ENABLED is off")

    monkeypatch.setattr("tools.coding.lsp.default_runner", _spy_runner)
    (tmp_path / "x.py").write_text("a = 1\n")
    t = _tool(tmp_path)
    res = await t.str_replace(
        StrReplaceParams(file_path="x.py", old_string="a = 1", new_string="a = 2")
    )
    assert getattr(res, "error", None) in (None, "")
    assert res.extracted_content == "Edited x.py (1 replacement)."
    assert calls == []  # zero subprocess spawns


def test_confine_blocks_git_directory(tmp_path):
    """P1 (finalization): the coding tool must refuse to touch .git/* (a patched
    .git/config hooksPath or .git/hooks/* is an RCE persistence vector), parity
    with self_env._confine. The shadow-snapshot dir is named `git` (no dot)."""
    from tools.coding.tool import CodingError
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    t = object.__new__(CodingTool)
    for bad in (".git/hooks/pre-commit", ".git/config"):
        with pytest.raises(CodingError) as ei:
            t._confine(bad, str(tmp_path))
        assert ".git" in str(ei.value)
    # A normal in-root path is still allowed.
    ok = t._confine("src/app.py", str(tmp_path))
    assert ok.endswith("src/app.py")
