"""Brain-state must never reach stream consumers.

Regression for the kimi-k2.6 leak: ROB instructs every model to emit its brain
state as ``{"current_state": {...}}`` text content every turn.  On a tool-free
planning turn that content was streamed verbatim and surfaced as a raw JSON dump
in the CLI.  The fix filters brain-state at the single ``stream_output`` funnel
(covers CLI / API / WebView) and is provider-agnostic.
"""

from __future__ import annotations

import logging

import pytest

from agents.task.agent.hitl_manager import HITLManager
from agents.task.utils_json import is_brain_state_content

# --- captured shapes -------------------------------------------------------

_BRAIN_WRAPPED = (
    '{"current_state": {"evaluation_previous_goal": "N/A - starting fresh", '
    '"memory": "User wants a repo review.", '
    '"next_goal": "List the root directory, then explore key files.", '
    '"reasoning": "Understand layout first."}}'
)
_BRAIN_BARE = (
    '{"page_summary":"","evaluation_previous_goal":"Pending",'
    '"memory":"Synthesis pending","next_goal":"Read modules/llm","reasoning":"x"}'
)


# --- predicate -------------------------------------------------------------


def test_is_brain_state_content_detects_brain_json():
    assert is_brain_state_content(_BRAIN_WRAPPED)
    assert is_brain_state_content(_BRAIN_BARE)


def test_is_brain_state_content_passes_prose_and_non_brain_json():
    assert not is_brain_state_content("## Repo Review\n\nThe project is a FastAPI app.")
    assert not is_brain_state_content("Reading modules/llm/adapters.py now…")
    # A non-brain JSON object (e.g. a genuine tool/file payload) must stream.
    assert not is_brain_state_content('{"result": 42, "ok": true}')
    # Partial token chunk (doesn't parse) must stream so real streaming works.
    assert not is_brain_state_content('{"current_state": {"memory": "par')
    assert not is_brain_state_content("")
    assert not is_brain_state_content(None)


# --- stream_output funnel --------------------------------------------------


def _manager():
    return HITLManager(
        session_id="s", agent_id="a", logger=logging.getLogger("hitl-test")
    )


@pytest.mark.asyncio
async def test_stream_output_suppresses_brain_state():
    seen = []
    mgr = _manager()
    mgr.register_output_callback(lambda c: seen.append(c) or _noop())

    await mgr.stream_output(_BRAIN_WRAPPED)
    await mgr.stream_output("Here is the actual answer.")
    await mgr.stream_output(_BRAIN_BARE)

    # Only the prose chunk reaches consumers; both brain blobs are dropped.
    assert seen == ["Here is the actual answer."]


@pytest.mark.asyncio
async def test_stream_output_scrubs_mixed_blob_keeps_prose():
    # OR-7: DeepSeek shape — fenced brain + real prose. The brain is scrubbed; the
    # prose still reaches consumers (the legacy all-or-nothing guard streamed the
    # whole blob).
    seen = []
    mgr = _manager()
    mgr.register_output_callback(lambda c: seen.append(c) or _noop())

    blob = "```json\n" + _BRAIN_WRAPPED + "\n```\n\nHey there! How can I help?"
    await mgr.stream_output(blob)

    assert len(seen) == 1
    assert "current_state" not in seen[0]
    assert "Hey there! How can I help?" in seen[0]


@pytest.mark.asyncio
async def test_stream_output_drops_truncated_brain():
    # OR-7: Qwen shape — stream cut mid-object (invalid JSON). Must still be dropped
    # (the legacy guard streamed it because json.loads failed).
    seen = []
    mgr = _manager()
    mgr.register_output_callback(lambda c: seen.append(c) or _noop())

    truncated = '{"page_summary":"","evaluation_previous_goal":"Success","memory":"x","next_goal":"Wait","phase":"'
    await mgr.stream_output(truncated)

    assert seen == []


async def _noop():
    return None
