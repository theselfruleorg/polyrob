"""The real false sentences of prod 2026-11-06, and sentences that must pass."""
import pytest

from agents.task.runtime.tool_claims import falsely_absent, refusal_for_step

REGISTERED = {"filesystem_write_file", "filesystem_append_file", "filesystem_jsonl_append",
              "twitter_get_tweets", "twitter_get_timeline", "twitter_search",
              "twitter_post", "twitter_quote", "twitter_reply", "done", "message"}


@pytest.mark.parametrize("text, expected", [
    ("I don't have a file-write tool in this session, so I can't append the ledger lines.",
     "filesystem_write_file"),
    ("Here are the three ledger lines to paste (I have no file-write tool this session):",
     "filesystem_jsonl_append"),
    ("Hero post is live. Ledger lines are in my earlier message — no file-write tool this session.",
     "filesystem_append_file"),
    ("Rung not posted: X read/post tools unavailable this session.", "twitter_get_tweets"),
    ("Rung not posted: X read/post tools unavailable this session.", "twitter_quote"),
    ("twitter_post is not available in this run.", "twitter_post"),
])
def test_the_false_sentences_name_the_tools(text, expected):
    assert expected in falsely_absent(text, REGISTERED)


@pytest.mark.parametrize("text", [
    "No post went out today: the cooldown is still active.",
    "There is no X post to quote yet.",
    "No new mentions. Nothing to do.",
    "I made no twitter_post call in this run.",
    "The file was written. No problems.",
    "Do you have no file-write tool?",
    "No file was done, and no message came.",
])
def test_other_negative_sentences_pass(text):
    assert falsely_absent(text, REGISTERED) == []


def test_a_tool_that_is_not_registered_is_a_true_statement():
    assert falsely_absent("I have no file-write tool.", {"done"}) == []


def test_a_tool_whose_last_call_failed_may_be_called_unavailable():
    text = "X read/post tools unavailable this session."
    left = falsely_absent(text, REGISTERED, errored=["twitter_get_tweets"])
    assert "twitter_get_tweets" not in left and "twitter_post" in left


def test_only_the_words_to_a_person_are_read():
    dump = [{"filesystem_write_file": {"text": "I have no file-write tool."}}]
    assert refusal_for_step(dump, REGISTERED) is None
    dump = [{"message": {"action": "read", "text": "I have no file-write tool."}}]
    assert refusal_for_step(dump, REGISTERED) is None
    dump = [{"done": {"text": "I have no file-write tool."}}]
    assert "filesystem_write_file" in refusal_for_step(dump, REGISTERED)


@pytest.mark.parametrize("text, refused", [
    ("placeholder", True), ("Placeholder.", True), ("  TODO ", True), ("<your text>", True),
    ("test", True), ("verify", True), ("A placeholder would not work here, so here is the real post.", False),
    ("In a week, 6,551 go live.", False), (None, False),
])
def test_a_placeholder_post_is_refused(text, refused):
    from polyrob_x.post_spacing import placeholder_refusal
    assert bool(placeholder_refusal("twitter_post", text)) is refused
