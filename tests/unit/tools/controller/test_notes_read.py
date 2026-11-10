"""The notes read actions (list, show) name a read failure: an unreadable notes store
is not "No notes yet" and is not "Note not found"."""
import asyncio

import pytest

from tools.controller.notes_read import note_read_result


def _fmt(ts):
    return "2026-01-01"


class _Prov:
    def __init__(self, fail=()):
        self.fail = set(fail)

    async def note_list(self, user_id, status="active"):
        if "list" in self.fail or (status == "pending" and "pending" in self.fail):
            raise RuntimeError("database is locked")
        if status == "pending":
            return []
        return [{"id": 1, "title": "Alpha", "tags": ["x"], "updated_ts": 1, "content": "body"}]

    async def note_get(self, user_id, note_id):
        if "get" in self.fail:
            raise RuntimeError("database is locked")
        return {"id": note_id, "title": "Alpha", "tags": [], "status": "active",
                "created_ts": 1, "content": "body", "links": []}

    async def note_backlinks(self, user_id, title):
        if "backlinks" in self.fail:
            raise RuntimeError("database is locked")
        return [{"id": 2, "title": "Beta"}]


@pytest.mark.parametrize("action,fail", [
    ("list", {"list"}), ("list", {"pending"}), ("show", {"get"}), ("show", {"backlinks"}),
])
def test_a_read_failure_is_named_not_raised(action, fail):
    r = asyncio.run(note_read_result(_Prov(fail), "u1", action, 1, _fmt))
    assert r.error and "could not be read" in r.error
    assert r.include_in_memory is False


def test_the_reads_still_answer():
    listed = asyncio.run(note_read_result(_Prov(), "u1", "list", None, _fmt))
    assert "#1 Alpha [x]" in listed.extracted_content
    shown = asyncio.run(note_read_result(_Prov(), "u1", "show", 1, _fmt))
    assert "backlinked from: #2 Beta" in shown.extracted_content
    assert "requires note_id" in asyncio.run(
        note_read_result(_Prov(), "u1", "show", None, _fmt)).extracted_content
