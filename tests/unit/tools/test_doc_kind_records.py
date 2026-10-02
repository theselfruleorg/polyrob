"""060 WS-3 — INSTRUCTION vs RECORD on workspace documents.

2026-09-21: the agent judged that `x-post-queue.md` was a dated log and that a
rewrite would falsify it — with no marker on disk. Now the marker exists,
undeclared = record (owner decision Q2), and the filesystem verbs refuse a
rewrite or delete of a record (an append, a new file, the session's own draft
and the one-step `kind: instruction` declaration are allowed).
"""
import logging
import os

import pytest

from core.doc_kind import (KIND_INSTRUCTION, KIND_RECORD, classify_tree, doc_kind,
                           is_declared, parse_front_matter, record_write_refusal,
                           supersedes)
from tools.filesystem import AppendFileAction, DeleteFileAction, FileSystem, WriteFileAction

LOG = "# X post queue\n\n- 2026-09-20 posted A\n"
INSTR = "---\nkind: instruction\nsupersedes: old-rules.md\n---\n# Trading rules\n\n- never > $5\n"


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
	monkeypatch.delenv("DOC_KIND_ENFORCED", raising=False)


def test_front_matter_is_read():
	fields, body = parse_front_matter(INSTR)
	assert fields["kind"] == "instruction" and body.startswith("# Trading rules")
	assert supersedes(INSTR) == ["old-rules.md"]
	assert parse_front_matter(LOG) == ({}, LOG)


def test_undeclared_is_a_record():
	assert doc_kind(LOG) == KIND_RECORD and not is_declared(LOG)
	assert doc_kind("---\nkind: banana\n---\nx") == KIND_RECORD
	assert doc_kind(INSTR) == KIND_INSTRUCTION


@pytest.mark.parametrize("old,new,ok", [
	(None, "anything", True),                         # a new file
	(LOG, LOG + "- 2026-09-21 posted B\n", True),       # an append
	(LOG, LOG.replace("posted A", "posted Z"), False),  # a rewrite of history
	(LOG, None, False),                                 # a delete
	(INSTR, INSTR.replace("$5", "$3"), True),           # an instruction doc
	(LOG, "---\nkind: instruction\n---\n" + LOG, True),  # the one-step declaration
])
def test_record_write_refusal(old, new, ok):
	reason = record_write_refusal("reports/x.md", old, new)
	assert (reason is None) == ok, reason
	if not ok:
		assert "RECORD" in reason and "append" in reason.lower() and "kind: instruction" in reason


def test_only_documents_and_only_when_enforced(monkeypatch):
	assert record_write_refusal("data.json", LOG, "x") is None
	assert record_write_refusal("x.md", LOG, "x", written_this_session=True) is None
	monkeypatch.setenv("DOC_KIND_ENFORCED", "false")
	assert record_write_refusal("x.md", LOG, "x") is None


def test_classify_tree(tmp_path):
	(tmp_path / "reports").mkdir()
	(tmp_path / "reports" / "queue.md").write_text(LOG)
	(tmp_path / "reports" / "rules.md").write_text(INSTR)
	(tmp_path / "reports" / "done.md").write_text("---\nkind: record\n---\nx")
	(tmp_path / "code.py").write_text("x = 1")
	out = classify_tree(str(tmp_path))
	assert (out["instruction"], out["record"], out["undeclared"]) == (1, 1, 1)
	assert out["instruction_docs"] == [os.path.join("reports", "rules.md")]
	assert out["superseded"] == [os.path.join("reports", "old-rules.md")]
	assert classify_tree(str(tmp_path / "nope")) == {"state": "no persistent workspace found"}


# --- the filesystem verbs ------------------------------------------------------

def _fs(session="s1"):
	t = object.__new__(FileSystem)
	t.logger = logging.getLogger("fs-doc-kind")
	t.session_id = session
	t.user_id = "u1"
	t._current_session_id = None
	t._enabled = True
	return t


async def _noop():
	return None


@pytest.fixture
def _pm(tmp_path, monkeypatch):
	from agents.task.path import PathManager, set_path_manager
	set_path_manager(PathManager(data_root=str(tmp_path / "data")))
	monkeypatch.setattr(FileSystem, "ensure_initialized", lambda self: _noop(), raising=False)


def _plant(t, rel, text):
	path = t._normalize_path(rel)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w") as fh:
		fh.write(text)
	return path


@pytest.mark.asyncio
async def test_rewrite_of_an_earlier_record_is_refused(_pm):
	t = _fs("s-record-1")
	path = _plant(t, "reports/queue.md", LOG)
	with pytest.raises(Exception) as ei:
		await t.write_file(WriteFileAction(file_path="reports/queue.md", content="rewritten\n"))
	assert "RECORD" in str(ei.value)
	assert open(path).read() == LOG
	with pytest.raises(Exception):
		await t.delete_file(DeleteFileAction(file_path="reports/queue.md"))
	assert os.path.exists(path)


@pytest.mark.asyncio
async def test_append_and_own_draft_are_allowed(_pm):
	t = _fs("s-record-2")
	path = _plant(t, "reports/queue.md", LOG)
	await t.append_file(AppendFileAction(file_path="reports/queue.md", content="- B\n"))
	assert open(path).read().endswith("- B\n")
	await t.write_file(WriteFileAction(file_path="reports/draft.md", content="v1\n"))
	await t.write_file(WriteFileAction(file_path="reports/draft.md", content="v2\n"))
	assert open(t._normalize_path("reports/draft.md")).read() == "v2\n"


@pytest.mark.asyncio
async def test_the_declaration_unlocks_edits(_pm):
	t = _fs("s-record-3")
	path = _plant(t, "reports/rules.md", "- rule one\n")
	await t.write_file(WriteFileAction(file_path="reports/rules.md",
	                                   content="---\nkind: instruction\n---\n- rule one\n"))
	from tools import filesystem_doc_kind
	filesystem_doc_kind._WRITES.clear()  # as if a later session: no own-draft exemption
	await t.write_file(WriteFileAction(file_path="reports/rules.md",
	                                    content="---\nkind: instruction\n---\n- rule two\n"))
	assert "rule two" in open(path).read()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "- New entry\n"])
async def test_writing_existing_record_does_not_make_it_an_own_draft(_pm, suffix):
    t = _fs("s-record-no-laundering-" + str(bool(suffix)))
    path = _plant(t, "reports/queue.md", LOG)
    await t.write_file(WriteFileAction(file_path="reports/queue.md", content=LOG + suffix))
    with pytest.raises(Exception, match="RECORD"):
        await t.write_file(WriteFileAction(file_path="reports/queue.md", content="lost history\n"))
    with pytest.raises(Exception, match="RECORD"):
        await t.delete_file(DeleteFileAction(file_path="reports/queue.md"))
    assert open(path).read() == LOG + suffix


@pytest.mark.asyncio
async def test_copying_over_existing_record_does_not_make_it_an_own_draft(_pm):
    from tools.filesystem import CopyFileAction
    t = _fs("s-record-copy-no-laundering")
    path = _plant(t, "reports/queue.md", LOG)
    _plant(t, "reports/copy.md", LOG + "- New entry\n")
    await t.copy_file(CopyFileAction(source_path="reports/copy.md",
                                    dest_path="reports/queue.md", overwrite=True))
    with pytest.raises(Exception, match="RECORD"):
        await t.write_file(WriteFileAction(file_path="reports/queue.md", content="lost history\n"))
    assert open(path).read() == LOG + "- New entry\n"
