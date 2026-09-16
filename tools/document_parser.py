"""Bounded PDF/DOCX subprocesses; no fallback into the application interpreter."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from tools.code_exec.backends.bounded_capture import capture
from tools.document_worker import MAX_INPUT_BYTES, MAX_TEXT_CHARS

TIMEOUT_SECONDS = 15
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
_slots = threading.BoundedSemaphore(2)


class DocumentParseError(ValueError):
    pass


def _reserve(kind, content):
    if kind not in ('pdf', 'docx') or not isinstance(content, bytes):
        raise DocumentParseError('unsupported document input')
    if len(content) > MAX_INPUT_BYTES:
        raise DocumentParseError('document input budget exceeded')
    if not _slots.acquire(blocking=False):
        raise DocumentParseError('document parser busy; retry later')


def _parse_reserved(kind, content, stop):
    try:
        if stop.is_set():
            raise DocumentParseError('document parsing cancelled')
        with tempfile.TemporaryDirectory(prefix='polyrob-document-') as workdir:
            proc = subprocess.Popen(
                [sys.executable, '-I', str(Path(__file__).resolve().with_name('document_worker.py')), kind],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=workdir, env={}, close_fds=True,
            )
            def kill(child):
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
            out, _, timed_out, overflow = capture(
                proc, data=content, timeout=TIMEOUT_SECONDS, limit=MAX_OUTPUT_BYTES,
                stop=stop, kill=kill,
            )
            if proc.returncode or timed_out or overflow or stop.is_set():
                raise DocumentParseError('document parsing refused or resource limit reached')
            result = json.loads(out)
            if (not isinstance(result, dict) or not isinstance(result.get('content'), str)
                    or len(result['content']) > MAX_TEXT_CHARS):
                raise DocumentParseError('invalid document parser response')
            return result
    finally:
        _slots.release()


def parse_document(kind, content):
    _reserve(kind, content)
    return _parse_reserved(kind, content, threading.Event())


async def parse_document_async(kind, content):
    _reserve(kind, content)
    stop = threading.Event()
    try:
        future = asyncio.get_running_loop().run_in_executor(None, _parse_reserved, kind, content, stop)
    except BaseException:
        _slots.release()
        raise
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        stop.set()
        # The thread owns the slot until its child has exited. Repeated task
        # cancellation must not orphan a parser or suppress the first cancel.
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if future.done() and not future.cancelled():
            future.exception()
        raise
