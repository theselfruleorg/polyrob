"""EditingProgressReporter: transport-pure status state machine (send→edit→delete),
idempotent + fail-open."""
import pytest

from core.surfaces.progress import (
    EditingProgressReporter,
    NullProgressReporter,
    ProgressStage,
)


class _Recorder:
    """Records send/edit/delete calls; send returns incrementing ids."""

    def __init__(self, *, raise_on=None, send_returns_none=False):
        self.sends, self.edits, self.deletes = [], [], []
        self._next = 0
        self._raise_on = raise_on
        self._send_none = send_returns_none

    async def send(self, text):
        if self._raise_on == "send":
            raise RuntimeError("boom")
        self.sends.append(text)
        if self._send_none:
            return None
        self._next += 1
        return self._next

    async def edit(self, mid, text):
        if self._raise_on == "edit":
            raise RuntimeError("boom")
        self.edits.append((mid, text))

    async def delete(self, mid):
        if self._raise_on == "delete":
            raise RuntimeError("boom")
        self.deletes.append(mid)

    def reporter(self, **kw):
        return EditingProgressReporter(self.send, self.edit, self.delete, **kw)


@pytest.mark.asyncio
async def test_first_stage_sends_and_captures_id():
    r = _Recorder(); rep = r.reporter()
    await rep.stage("A")
    assert r.sends == ["A"] and r.edits == [] and r.deletes == []


@pytest.mark.asyncio
async def test_second_stage_edits_in_place():
    r = _Recorder(); rep = r.reporter()
    await rep.stage("A"); await rep.stage("B")
    assert r.sends == ["A"] and r.edits == [(1, "B")] and r.deletes == []


@pytest.mark.asyncio
async def test_identical_text_is_noop():
    r = _Recorder(); rep = r.reporter()
    await rep.stage("A"); await rep.stage("A")
    assert r.sends == ["A"] and r.edits == []


@pytest.mark.asyncio
async def test_finish_deletes_captured_id():
    r = _Recorder(); rep = r.reporter()
    await rep.stage("A"); await rep.finish()
    assert r.deletes == [1]


@pytest.mark.asyncio
async def test_finish_without_stage_is_noop():
    r = _Recorder(); rep = r.reporter()
    await rep.finish()
    assert r.deletes == []


@pytest.mark.asyncio
async def test_finish_is_idempotent():
    r = _Recorder(); rep = r.reporter()
    await rep.stage("A"); await rep.finish(); await rep.finish()
    assert r.deletes == [1]


@pytest.mark.asyncio
async def test_stage_after_finish_is_noop():
    r = _Recorder(); rep = r.reporter()
    await rep.stage("A"); await rep.finish(); await rep.stage("late")
    assert r.sends == ["A"] and r.edits == []


@pytest.mark.asyncio
async def test_supports_edit_false_sends_fresh_note():
    r = _Recorder(); rep = r.reporter(supports_edit=False)
    await rep.stage("A"); await rep.stage("B")
    assert r.sends == ["A", "B"] and r.edits == []
    await rep.finish()
    assert r.deletes == [2]  # deletes the latest


@pytest.mark.asyncio
async def test_send_failure_fail_open_then_retries_send():
    r = _Recorder(raise_on="send"); rep = r.reporter()
    await rep.stage("A")  # swallowed, no id captured
    r._raise_on = None
    await rep.stage("B")  # retries a send (not an edit)
    assert r.sends == ["B"] and r.edits == []


@pytest.mark.asyncio
async def test_edit_failure_fail_open():
    r = _Recorder(raise_on="edit"); rep = r.reporter()
    await rep.stage("A"); await rep.stage("B")  # edit raises, swallowed
    assert r.sends == ["A"]  # no crash


@pytest.mark.asyncio
async def test_delete_failure_fail_open():
    r = _Recorder(raise_on="delete"); rep = r.reporter()
    await rep.stage("A"); await rep.finish()  # delete raises, swallowed
    assert rep._finished is True


@pytest.mark.asyncio
async def test_none_id_then_finish_skips_delete():
    r = _Recorder(send_returns_none=True); rep = r.reporter()
    await rep.stage("A"); await rep.finish()
    assert r.deletes == []


@pytest.mark.asyncio
async def test_null_reporter_noop():
    rep = NullProgressReporter()
    await rep.stage("x"); await rep.finish()  # no transport, no raise


def test_stage_presets_exact():
    assert ProgressStage.TRANSCRIBING == "🎤 Transcribing your voice message…"
    assert ProgressStage.WORKING == "⚙️ Working…"
