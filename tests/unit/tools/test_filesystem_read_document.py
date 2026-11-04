"""filesystem_read_file returns the extracted TEXT of a .pdf / .docx.

Before, every file was decoded as UTF-8 with errors='replace', so an owner's PDF
reached the agent as replacement-character noise while the attachment hint told
it to use exactly this verb. The read now goes through the bounded parser
subprocess (tools/document_parser.py) and the usual line windows page the text.
"""
import io
import logging
import os

import pytest

from core.exceptions import ServiceError
from tools.filesystem import FileSystem, ReadFileAction


def _fs_tool():
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-read-document")
    t.session_id = "s1"
    t.user_id = "u1"
    t._current_session_id = None
    t._enabled = True
    return t


@pytest.fixture
def _tool(tmp_path, monkeypatch):
    from agents.task.path import PathManager, set_path_manager
    set_path_manager(PathManager(data_root=str(tmp_path / "data")))

    async def _noop(self):
        return None
    monkeypatch.setattr(FileSystem, "ensure_initialized", _noop, raising=False)
    return _fs_tool()


def _write_bytes(tool, name, data):
    path = tool._normalize_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _docx_bytes(paragraphs):
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_docx_is_read_as_its_paragraph_text(_tool):
    _write_bytes(_tool, "letter.docx", _docx_bytes(["Dear reader,", "The total is 42."]))

    out = await _tool.read_file(ReadFileAction(file_path="letter.docx"))

    assert "Dear reader," in out
    assert "The total is 42." in out
    assert "�" not in out


@pytest.mark.asyncio
async def test_a_document_is_paged_by_the_line_window(_tool):
    _write_bytes(_tool, "long.docx", _docx_bytes([f"line {i}" for i in range(1, 11)]))

    out = await _tool.read_file(ReadFileAction(file_path="long.docx", offset=3, limit=2))

    assert out.startswith("[lines 3-4 of 10]")
    assert "line 3" in out and "line 4" in out and "line 5" not in out


@pytest.mark.asyncio
async def test_pdf_routes_through_the_bounded_parser(_tool, monkeypatch):
    seen = {}

    async def fake_parse(kind, content):
        seen["kind"] = kind
        seen["head"] = content[:8]
        return {"content": "Page one text\n\nPage two text"}
    import tools.document_parser as parser
    monkeypatch.setattr(parser, "parse_document_async", fake_parse)
    _write_bytes(_tool, "report.pdf", b"%PDF-1.4 binary \xff\xfe body")

    out = await _tool.read_file(ReadFileAction(file_path="report.pdf"))

    assert seen == {"kind": "pdf", "head": b"%PDF-1.4"}
    assert out == "Page one text\n\nPage two text"


@pytest.mark.asyncio
async def test_a_scanned_pdf_says_there_is_no_text_layer(_tool, monkeypatch):
    async def fake_parse(kind, content):
        return {"content": "\n\n  \n"}
    import tools.document_parser as parser
    monkeypatch.setattr(parser, "parse_document_async", fake_parse)
    _write_bytes(_tool, "scan.pdf", b"%PDF-1.4")

    out = await _tool.read_file(ReadFileAction(file_path="scan.pdf"))

    assert "no text could be extracted" in out
    assert "no OCR" in out


@pytest.mark.asyncio
async def test_a_missing_parser_names_the_remedy(_tool, monkeypatch):
    import tools.document_parser as parser

    async def refuse(kind, content):
        raise parser.DocumentParseError(
            "pdf parsing is unavailable: install polyrob[docs]")
    monkeypatch.setattr(parser, "parse_document_async", refuse)
    _write_bytes(_tool, "report.pdf", b"%PDF-1.4")

    with pytest.raises(ServiceError) as exc:
        await _tool.read_file(ReadFileAction(file_path="report.pdf"))

    assert "polyrob[docs]" in str(exc.value)


@pytest.mark.asyncio
async def test_an_oversize_document_is_refused_before_the_parser(_tool, monkeypatch):
    import tools.document_worker as worker
    import tools.document_parser as parser
    monkeypatch.setattr(worker, "MAX_INPUT_BYTES", 16)

    async def must_not_run(kind, content):
        pytest.fail("parser ran on an over-limit file")
    monkeypatch.setattr(parser, "parse_document_async", must_not_run)
    _write_bytes(_tool, "big.pdf", b"%PDF-1.4" + b"x" * 64)

    with pytest.raises(ServiceError) as exc:
        await _tool.read_file(ReadFileAction(file_path="big.pdf"))

    assert "document read limit" in str(exc.value)


@pytest.mark.asyncio
async def test_a_text_file_still_reads_verbatim(_tool):
    _write_bytes(_tool, "notes.md", b"# Title\n\n    indented\n")

    out = await _tool.read_file(ReadFileAction(file_path="notes.md"))

    assert out == "# Title\n\n    indented\n"
