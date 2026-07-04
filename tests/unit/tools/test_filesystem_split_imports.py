"""Import-stability tests for the filesystem.py split.

Asserts:
1. `from tools.filesystem import FileSystem` still works (external contract).
2. The extracted mixin modules are importable from their new locations.
3. PdfExtractionMixin and DocProcessingMixin are actually composed into FileSystem.
4. None of the three modules carry `from __future__ import annotations` as an
   actual import statement (which would break the registry's annotation inspection).
5. FileSystem still exposes all expected @action method names.
"""

import ast
import importlib
import inspect
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. External import contract
# ---------------------------------------------------------------------------

def test_filesystem_import_works():
    """from tools.filesystem import FileSystem must not raise."""
    from tools.filesystem import FileSystem  # noqa: F401
    assert FileSystem is not None


def test_filesystem_class_is_importable():
    """FileSystem is a class with expected base."""
    from tools.filesystem import FileSystem
    from tools.base_tool import BaseTool
    assert issubclass(FileSystem, BaseTool)


# ---------------------------------------------------------------------------
# 2. New modules are importable
# ---------------------------------------------------------------------------

def test_pdf_extractor_importable():
    from tools.filesystem_pdf import PdfExtractionMixin  # noqa: F401
    assert PdfExtractionMixin is not None


def test_docproc_importable():
    from tools.filesystem_docproc import DocProcessingMixin  # noqa: F401
    assert DocProcessingMixin is not None


# ---------------------------------------------------------------------------
# 3. Mixins are composed into FileSystem
# ---------------------------------------------------------------------------

def test_filesystem_inherits_pdf_mixin():
    from tools.filesystem import FileSystem
    from tools.filesystem_pdf import PdfExtractionMixin
    assert issubclass(FileSystem, PdfExtractionMixin)


def test_filesystem_inherits_docproc_mixin():
    from tools.filesystem import FileSystem
    from tools.filesystem_docproc import DocProcessingMixin
    assert issubclass(FileSystem, DocProcessingMixin)


def test_mro_order():
    """PdfExtractionMixin and DocProcessingMixin both in MRO before BaseTool."""
    from tools.filesystem import FileSystem
    from tools.filesystem_pdf import PdfExtractionMixin
    from tools.filesystem_docproc import DocProcessingMixin
    from tools.base_tool import BaseTool

    mro = FileSystem.__mro__
    idx_pdf = mro.index(PdfExtractionMixin)
    idx_doc = mro.index(DocProcessingMixin)
    idx_base = mro.index(BaseTool)
    assert idx_pdf < idx_base
    assert idx_doc < idx_base


# ---------------------------------------------------------------------------
# 4. No `from __future__ import annotations` as an actual import statement
# ---------------------------------------------------------------------------

_SPLIT_MODULES = [
    "tools/filesystem.py",
    "tools/filesystem_pdf.py",
    "tools/filesystem_docproc.py",
]


def _find_future_annotations_import(source: str) -> bool:
    """Return True if source has an actual `from __future__ import annotations` statement."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == '__future__':
                for alias in node.names:
                    if alias.name == 'annotations':
                        return True
    return False


@pytest.mark.parametrize("rel_path", _SPLIT_MODULES)
def test_no_future_annotations(rel_path):
    """None of the split modules may import annotations from __future__."""
    # Walk up from this file to find the project root
    here = Path(__file__).resolve()
    # tests/unit/tools/ -> tests/unit/ -> tests/ -> root
    project_root = here.parent.parent.parent.parent
    module_path = project_root / rel_path
    assert module_path.exists(), f"Module not found: {module_path}"
    source = module_path.read_text(encoding="utf-8")
    assert not _find_future_annotations_import(source), (
        f"{rel_path} contains `from __future__ import annotations` which breaks "
        "the registry's annotation inspection."
    )


# ---------------------------------------------------------------------------
# 5. FileSystem has expected @action method names
# ---------------------------------------------------------------------------

_EXPECTED_ACTIONS = [
    "extract_urls",
    "read_file",
    "write_file",
    "append_file",
    "list_directory",
    "delete_file",
    "create_directory",
]


@pytest.mark.parametrize("action_name", _EXPECTED_ACTIONS)
def test_filesystem_has_action(action_name):
    from tools.filesystem import FileSystem
    assert hasattr(FileSystem, action_name), (
        f"FileSystem is missing expected action method: {action_name}"
    )
    method = getattr(FileSystem, action_name)
    assert callable(method)


# ---------------------------------------------------------------------------
# 6. PDF helper methods available on FileSystem via mixin
# ---------------------------------------------------------------------------

_EXPECTED_PDF_METHODS = [
    "_process_pdf",
    "_read_pdf_with_recovery",
    "_read_pdf_permissive",
    "_read_pdf_advanced_recovery",
    "_extract_page_text_with_fallbacks",
    "_extract_textual_objects",
    "_clean_pdf_text",
    "_post_process_pdf_text",
    "_is_meaningful_content",
    "_contains_binary_data",
    "_is_readable_text",
    "_aggressive_binary_cleanup",
    "_extract_raw_pdf_text",
]


@pytest.mark.parametrize("method_name", _EXPECTED_PDF_METHODS)
def test_filesystem_has_pdf_method(method_name):
    from tools.filesystem import FileSystem
    assert hasattr(FileSystem, method_name), (
        f"FileSystem is missing PDF method: {method_name}"
    )


# ---------------------------------------------------------------------------
# 7. DocProcessing helper methods available on FileSystem via mixin
# ---------------------------------------------------------------------------

_EXPECTED_DOCPROC_METHODS = [
    "process_document",
    "_basic_process",
    "_extract_web_metadata",
    "_extract_main_content",
    "_get_cached_result",
    "_cache_result",
    "_clean_text",
    "analyze_document",
    "_get_analysis_prompt",
    "_parse_analysis_response",
    # process_url / process_web_content / the SSL-context helpers were removed:
    # dead, undecorated, SSRF-unsafe URL fetchers superseded by the web_fetch tool
    # (docs/plans/2026-06-29-web-fetch-tier1-IMPLEMENTATION-PLAN.md, Task 8).
]


@pytest.mark.parametrize("method_name", _EXPECTED_DOCPROC_METHODS)
def test_filesystem_has_docproc_method(method_name):
    from tools.filesystem import FileSystem
    assert hasattr(FileSystem, method_name), (
        f"FileSystem is missing doc-processing method: {method_name}"
    )
