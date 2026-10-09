"""A turn never says the same thing to the user twice.

Prod 2026-10-06 11:39:54 / 11:39:58 (session d34d332e): the model called
`send_message` with a reply, got "Message sent", then on the next step called
it again with the IDENTICAL text — and the owner received it twice. The
autonomous delivery rail dedups (USER_DELIVERY_DEDUP_HOURS); the interactive
turn had no guard at all.
"""
from pathlib import Path
from types import SimpleNamespace

from core.surfaces import turn_reply

ROOT = Path(__file__).resolve().parents[4]


def test_identical_text_in_the_same_turn_is_a_repeat():
    orch = SimpleNamespace()
    turn_reply.mark_reply_published(orch, "Locked in. Next run is 12:00.")
    assert turn_reply.is_repeat_reply(orch, "Locked in. Next run is 12:00.")
    # whitespace-only differences are still the same message
    assert turn_reply.is_repeat_reply(orch, "  Locked in. Next run is 12:00.\n")


def test_a_lightly_reworded_resend_is_a_repeat():
    """Prod 13:32:57 / 13:33:01: same correction, one sentence reworded."""
    first = ("You're right — three claims in it were false. Fixed:\n\n1. \"the token this account "
             "runs on\" — wrong. I run on the POLYROB framework, not on PNL.\n2. \"the thing that "
             "pays for the work\" — wrong. The work is paid from org funding, and buybacks stopped "
             "yesterday.\n3. \"I publish the ledger\" — wrong. I publish trading results, never balances.")
    second = first.replace("The work is paid from org funding, and buybacks stopped yesterday.",
                           "The work is funded by the org, not by PNL. The buyback ladder is off.")
    orch = SimpleNamespace()
    turn_reply.mark_reply_published(orch, first)
    assert turn_reply.is_repeat_reply(orch, second)


def test_short_or_genuinely_new_replies_are_not_repeats():
    orch = SimpleNamespace()
    turn_reply.mark_reply_published(orch, "Done.")
    assert not turn_reply.is_repeat_reply(orch, "Done!")  # short: exact match only
    turn_reply.mark_reply_published(orch, "Posted the den warning and muted the spammer for 24h. "
                                          "Nothing else needed from you right now.")
    assert not turn_reply.is_repeat_reply(orch, "Here is the draft for the 15:00 promo slot: "
                                                "6551 accounts give every agent a wallet on day one.")


def test_different_text_or_a_new_turn_is_not_a_repeat():
    orch = SimpleNamespace()
    turn_reply.mark_reply_published(orch, "first")
    assert not turn_reply.is_repeat_reply(orch, "second")
    turn_reply.reset_turn(orch)
    assert not turn_reply.is_repeat_reply(orch, "first")


def test_nothing_recorded_or_no_orchestrator_is_never_a_repeat():
    assert not turn_reply.is_repeat_reply(SimpleNamespace(), "hi")
    assert not turn_reply.is_repeat_reply(None, "hi")
    assert not turn_reply.is_repeat_reply(SimpleNamespace(), "")


def test_send_message_checks_for_a_repeat_before_it_publishes():
    src = (ROOT / "tools" / "controller" / "action_registration.py").read_text()
    start = src.index("async def send_message(params: SendMessageAction")
    body = src[start:src.index("async def done(", start)]
    assert "is_repeat_reply" in body
    assert body.index("is_repeat_reply") < body.index("build_discrete_publish")


def test_repeat_note_never_orders_done_mid_task():
    """Two identical timeout notices (llm_runner recovery) in one turn hit the
    repeat guard mid-task; the note must not tell the agent to end the turn."""
    assert "Call done() now" not in turn_reply.REPEAT_REPLY_NOTE
    assert "Continue the task" in turn_reply.REPEAT_REPLY_NOTE
    src = (ROOT / "tools/controller/action_registration.py").read_text(encoding="utf-8")
    assert "extracted_content=REPEAT_REPLY_NOTE" in src
