"""A1: the per-family model notes teach the ONE reply verb (send_message).

done(text) is never delivered to a user, so a note that says "reply with
done(text=...)" makes an answer vanish on that model family. The GPT note also
used to demand a function call on EVERY step, contradicting the planning-turn
allowance the rules block grants.
"""
from agents.task.constants import (
    GEMINI_FAMILY_INSTRUCTIONS,
    KIMI_FAMILY_INSTRUCTIONS,
    OPENAI_FAMILY_INSTRUCTIONS,
)

NOTES = (GEMINI_FAMILY_INSTRUCTIONS, OPENAI_FAMILY_INSTRUCTIONS, KIMI_FAMILY_INSTRUCTIONS)


def test_every_note_replies_with_send_message():
    for note in NOTES:
        assert "send_message(text=" in note
        assert "never reads it" in note


def test_no_note_teaches_done_as_the_reply():
    for note in NOTES:
        assert "done(text=" not in note
        assert "prefer done()" not in note


def test_gpt_note_keeps_the_planning_turn_allowance():
    assert "Every step must include" not in OPENAI_FAMILY_INSTRUCTIONS
    assert "optional brief planning" in OPENAI_FAMILY_INSTRUCTIONS
