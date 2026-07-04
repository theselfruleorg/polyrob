from modules.llm.messages import MessageOrigin, make_control_message


def test_compaction_summary_origin_and_envelope():
    assert MessageOrigin.COMPACTION_SUMMARY == "compaction_summary"
    msg = make_control_message("hello", MessageOrigin.COMPACTION_SUMMARY)
    assert msg.origin == MessageOrigin.COMPACTION_SUMMARY
    assert msg.content.startswith("<compacted-history>")
    assert "hello" in msg.content


def test_prior_summary_detected_after_envelope():
    """A wrapped prior summary must still be recognized so iterative compaction
    UPDATES it instead of re-summarizing it blind."""
    from agents.task.agent.messages.compactor import _COMPACTED_MARKER
    from modules.llm.messages import make_control_message, MessageOrigin
    body = f"{_COMPACTED_MARKER}\n\nsummary text"
    msg = make_control_message(body, MessageOrigin.COMPACTION_SUMMARY)
    text = str(msg.content)
    assert (getattr(msg, "origin", "") == MessageOrigin.COMPACTION_SUMMARY
            or _COMPACTED_MARKER in text[:160])
