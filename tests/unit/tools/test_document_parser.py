"""Real document worker and hostile worker lifecycle/resource regressions."""
import asyncio
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from tools import document_parser as parser
from tools import document_worker as worker


def pdf_bytes(pages=1):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 10 10 Td (Bounded PDF text) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_real_pdf_worker_extracts_and_caps_pages():
    result = parser.parse_document('pdf', pdf_bytes())
    assert 'Bounded PDF text' in result['content']
    assert result['metadata']['total_pages'] == 1
    with pytest.raises(parser.DocumentParseError):
        parser.parse_document('pdf', pdf_bytes(worker.MAX_PAGES + 1))


def test_docx_worker_uses_archive_admission():
    from zipfile import ZipFile
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr('word/document.xml', '<!DOCTYPE a [<!ENTITY x "boom">]><a/>')
    with pytest.raises(parser.DocumentParseError):
        parser.parse_document('docx', output.getvalue())


def test_limit_install_failure_refuses_before_parsing(monkeypatch, capsys):
    def fail():
        raise OSError('resource policy unavailable')
    monkeypatch.setattr(worker, 'apply_limits', fail)
    monkeypatch.setattr(worker, 'parse_document', lambda *args: pytest.fail('must not parse'))
    assert worker.main() == 1
    assert 'refused' in capsys.readouterr().err


def test_oversized_input_refused_before_launch(monkeypatch):
    monkeypatch.setattr(parser, 'MAX_INPUT_BYTES', 4)
    monkeypatch.setattr(parser.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('must not launch'))
    with pytest.raises(parser.DocumentParseError, match='input budget'):
        parser.parse_document('pdf', b'large')


def substitute_worker(monkeypatch, code):
    real = subprocess.Popen
    children = []
    started = threading.Event()
    def spawn(args, **kwargs):
        proc = real([sys.executable, '-I', '-c', code], **kwargs)
        children.append((proc, kwargs))
        started.set()
        return proc
    monkeypatch.setattr(parser.subprocess, 'Popen', spawn)
    return children, started


@pytest.mark.parametrize('code,output_cap', [
    ('import time; time.sleep(30)', 4096),
    ('import os; os.write(1, b"x"*100000); import time; time.sleep(30)', 1024),
    ('import os; os.write(2, b"x"*100000); import time; time.sleep(30)', 1024),
])
def test_timeout_and_both_output_streams_reap_worker(monkeypatch, code, output_cap):
    children, _ = substitute_worker(monkeypatch, code)
    monkeypatch.setattr(parser, 'TIMEOUT_SECONDS', .2)
    monkeypatch.setattr(parser, 'MAX_OUTPUT_BYTES', output_cap)
    with pytest.raises(parser.DocumentParseError):
        parser.parse_document('pdf', b'%PDF-test')
    proc, kwargs = children[0]
    assert proc.poll() is not None
    assert not Path(kwargs['cwd']).exists()
    assert all(stream.closed for stream in (proc.stdin, proc.stdout, proc.stderr))


def test_worker_receives_no_environment_or_workspace(monkeypatch):
    monkeypatch.setenv('AGENT_WALLET_MASTER_SEED', 'synthetic-test-only')
    code = 'import json,os,sys; sys.stdout.write(json.dumps({"content": str(sorted(os.environ)), "cwd_files": os.listdir(".")}))'
    children, _ = substitute_worker(monkeypatch, code)
    result = parser.parse_document('pdf', b'fake')
    assert 'AGENT_WALLET_MASTER_SEED' not in result['content']
    assert result['cwd_files'] == []
    assert children[0][1]['env'] == {}


@pytest.mark.asyncio
async def test_cancellation_reaps_child_and_restores_capacity(monkeypatch):
    children, started = substitute_worker(monkeypatch, 'import time; time.sleep(30)')
    monkeypatch.setattr(parser, '_slots', threading.BoundedSemaphore(1))
    task = asyncio.create_task(parser.parse_document_async('pdf', b'fake'))
    assert await asyncio.to_thread(started.wait, 5)
    with pytest.raises(parser.DocumentParseError, match='busy'):
        await parser.parse_document_async('pdf', b'fake')
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0][0].poll() is not None
    assert parser._slots.acquire(blocking=False)
    parser._slots.release()


@pytest.mark.asyncio
async def test_pdf_failure_never_uses_inprocess_recovery(monkeypatch):
    from tools.filesystem_pdf import PdfExtractionMixin
    from core.exceptions import ServiceError
    async def refuse(*args):
        raise parser.DocumentParseError('budget')
    monkeypatch.setattr(parser, 'parse_document_async', refuse)
    mixin = PdfExtractionMixin()
    mixin._read_pdf_with_recovery = lambda *args: pytest.fail('in-process fallback')
    with pytest.raises(ServiceError):
        await mixin._process_pdf(b'%PDF-1.4')


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='RLIMIT_AS enforcement requires Linux')
def test_linux_address_space_limit_rejects_allocation():
    script = (
        'import runpy; w=runpy.run_path(' + repr(str(Path(worker.__file__))) + '); '
        'w["apply_limits"](); bytearray(w["MEMORY_BYTES"] * 2)'
    )
    result = subprocess.run([sys.executable, '-I', '-c', script], env={}, capture_output=True, timeout=5)
    assert result.returncode != 0
    assert b'MemoryError' in result.stderr


def test_cpu_limit_terminates_busy_worker():
    script = (
        'import runpy; w=runpy.run_path(' + repr(str(Path(worker.__file__))) + '); '
        'w["apply_limits"].__globals__["CPU_SECONDS"]=1; w["apply_limits"]()\n'
        'while True: pass\n'
    )
    result = subprocess.run([sys.executable, '-I', '-c', script], env={}, capture_output=True, timeout=5)
    assert result.returncode < 0
