"""Phase 1.1 — store the ANSWER (distilled findings), not the Q+A transcript.

The cross-session store kept "User: {task}\nAssistant: {findings}" as one FTS row.
A recall query restates the task, so the indexed QUESTION text ranked as highly as
the answer (the GLM live-test symptom: question-echoes outrank answers).

With MEMORY_STORE_ANSWER_ONLY on, only the answer/findings are indexed+embedded; the
question is no longer matchable. Gated (default ON under POLYROB_LOCAL, OFF on the
server) so the multi-tenant baseline stays byte-identical until soaked.
"""
import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


def _provider(tmp_path):
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


@pytest.mark.asyncio
async def test_answer_only_kills_question_echo(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "1")
    p = _provider(tmp_path)
    await p.sync_turn("QUNIQUEQ how do I do the thing",
                      "ANSWERTOKEN run the deploy script",
                      session_id="s1", user_id="alice")
    # A query echoing only the QUESTION's unique token finds nothing now.
    assert await p.prefetch("QUNIQUEQ", session_id="s2", user_id="alice") == ""
    # A query matching the ANSWER still recalls it.
    recall = await p.prefetch("ANSWERTOKEN", session_id="s2", user_id="alice")
    assert "deploy script" in recall


@pytest.mark.asyncio
async def test_answer_only_excludes_question_from_stored_content(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "1")
    p = _provider(tmp_path)
    await p.sync_turn("the question text", "the answer text",
                      session_id="s1", user_id="alice")
    recall = await p.prefetch("answer", session_id="s2", user_id="alice")
    assert "the answer text" in recall
    assert "User:" not in recall
    assert "the question text" not in recall


@pytest.mark.asyncio
async def test_legacy_combined_storage_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_STORE_ANSWER_ONLY", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    p = _provider(tmp_path)
    await p.sync_turn("deploy plan", "ship to hetzner", session_id="s1", user_id="alice")
    # legacy: a question-word query still matches the combined row (byte-identical).
    recall = await p.prefetch("deploy", session_id="s2", user_id="alice")
    assert "hetzner" in recall


@pytest.mark.asyncio
async def test_answer_only_default_on_under_local_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_STORE_ANSWER_ONLY", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    p = _provider(tmp_path)
    await p.sync_turn("QUNIQUEQ task", "ANSWERTOKEN result", session_id="s1", user_id="alice")
    assert await p.prefetch("QUNIQUEQ", session_id="s2", user_id="alice") == ""
