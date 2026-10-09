import asyncio
import threading

import pytest

from modules.transcription import faster_whisper_transcriber as fw


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.asyncio
async def test_timeout_and_cancellation_keep_capacity_until_cpu_worker_finishes(monkeypatch, cancel):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(fw, "_SLOTS", slots)
    monkeypatch.setattr(fw, "INFERENCE_TIMEOUT_SECONDS", .02)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    transcriber = fw.FasterWhisperTranscriber()

    def inference(audio, language):
        calls.append(audio)
        started.set()
        try:
            assert release.wait(3)
            return "heard"
        finally:
            finished.set()

    monkeypatch.setattr(transcriber, "_transcribe_sync", inference)
    task = asyncio.create_task(transcriber.transcribe(b"first"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task == ""
        assert await transcriber.transcribe(b"second") == ""
        assert calls == [b"first"]
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        if not task.done():
            await task
    # Completion callbacks run just after the worker returns.
    for _ in range(100):
        if slots.acquire(blocking=False):
            slots.release()
            break
        await asyncio.sleep(.001)
    else:
        pytest.fail("completed inference never released capacity")


@pytest.mark.asyncio
async def test_oversized_audio_never_enters_inference(monkeypatch):
    monkeypatch.setattr(fw, "MAX_AUDIO_BYTES", 2)
    transcriber = fw.FasterWhisperTranscriber()
    monkeypatch.setattr(transcriber, "_transcribe_sync", lambda *a: pytest.fail("ran inference"))
    assert await transcriber.transcribe(b"abc") == ""


@pytest.mark.asyncio
async def test_a_busy_slot_does_not_starve_the_owner(monkeypatch):
    """CHAT-4: a member's long note holds the shared slot; the owner's voice
    takes its own slot, a second member's does not."""
    from core.surfaces.media_access import OWNER_VOICE
    shared = threading.BoundedSemaphore(1)
    shared.acquire()                                   # a member's inference is running
    monkeypatch.setattr(fw, "_SLOTS", shared)
    monkeypatch.setattr(fw, "_OWNER_SLOT", threading.BoundedSemaphore(1))
    transcriber = fw.FasterWhisperTranscriber()
    monkeypatch.setattr(transcriber, "_transcribe_sync", lambda audio, language: "owner heard")
    assert await transcriber.transcribe(b"member two") == ""
    token = OWNER_VOICE.set(True)
    try:
        assert await transcriber.transcribe(b"owner") == "owner heard"
    finally:
        OWNER_VOICE.reset(token)
        shared.release()
