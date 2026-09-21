"""Revalidation of the 2026-09-21 interface audit, Telegram seat.

Two honesty/reach gaps the audit's own changes created or left:

1. ``Verb.seats`` made `/gates` and `/meter` REPL-local and filtered them out of
   this seat's help body. `/help gates` then fell through to "Unknown command
   /gates" — a confident lie about a verb that exists. Asking about something
   real must never be answered as if it were a typo.

2. ``GoalBoard.decide_ask(answer=)`` let the owner ANSWER an ask in writing and
   carried the answer into the retry prompt — but only the CONSOLE could reach
   it. The phone, the seat the owner actually holds, still called
   ``fulfill_ask`` and had no way to type an answer at all.
"""
import os

import pytest

from surfaces.telegram.harness import _SEAT, _USAGE, _help_for


# --- /help for a verb this seat cannot run ---------------------------------

@pytest.mark.parametrize("verb", ["/gates", "/meter"])
def test_help_for_a_seat_local_verb_names_the_seat_not_a_typo(verb):
    from core.verbs import verb_for

    row = verb_for(verb)
    assert row is not None and not row.runs_on(_SEAT), (
        f"{verb} is no longer seat-local — this test is asserting nothing")
    out = _help_for(verb)
    assert "Unknown command" not in out
    assert verb in out
    for seat in row.seats:
        assert seat in out
    # It still says WHAT the verb is, so the answer is useful, not just polite.
    assert row.help.rstrip(".") in out


def test_help_for_a_real_nonsense_token_is_still_an_unknown_command():
    """The honest-seat answer must not swallow a genuine typo."""
    assert "Unknown command" in _help_for("/statsu")


# --- /fulfill carries the owner's answer -----------------------------------

class _Identity:
    def __init__(self, uid):
        self.user_id = uid
        self.raw_user_id = uid
        self.chat_role = None
        self.source = None


class _Inbound:
    def __init__(self, text, uid):
        self.text = text
        self.identity = _Identity(uid)


class _Decision:
    session_id = "s1"


class _Result:
    def __init__(self, text, uid):
        self.inbound = _Inbound(text, uid)
        self.decision = _Decision()


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, _name):
        return None


class _Agent:
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


def _board_with_blocked_goal(data_dir):
    from agents.task.goals.board import GoalBoard

    board = GoalBoard(os.path.join(data_dir, "goals.db"))
    goal = board.create(user_id="rob", title="ship it")
    board.claim(goal.id, "w", ttl_seconds=60)
    board.record_failure(goal.id, error="which account?")
    board.claim(goal.id, "w", ttl_seconds=60)
    board.record_failure(goal.id, error="which account?")  # breaker -> blocked
    ask = board.create_ask(user_id="rob", what="Which account?",
                           blocks_goal_ids=[goal.id])
    return board, goal, ask


@pytest.mark.asyncio
async def test_fulfill_passes_the_owners_written_answer_into_the_retry_prompt(
        tmp_path, monkeypatch):
    import surfaces.telegram.harness as h
    from agents.task.goals.board import GoalBoard

    monkeypatch.setattr(h, "_is_admin_owner", lambda _uid: True)
    board, goal, ask = _board_with_blocked_goal(str(tmp_path))

    out = await h._handle_owner_admin(
        _Agent(str(tmp_path)),
        _Result(f"/fulfill {ask.id} use the treasury account, not the hot wallet",
                "rob"),
        "/fulfill")
    assert out.startswith("✅ Ask fulfilled")
    assert "treasury account" in out

    reread = GoalBoard(os.path.join(str(tmp_path), "goals.db"))
    assert (reread.get(ask.id).payload or {}).get("answer") == (
        "use the treasury account, not the hot wallet")
    unblocked = (reread.get(goal.id).payload or {}).get("owner_unblocked") or {}
    assert unblocked.get("answer") == "use the treasury account, not the hot wallet"


@pytest.mark.asyncio
async def test_fulfill_without_an_answer_is_byte_compatible(tmp_path, monkeypatch):
    """The bare form must keep working and must NOT stamp an empty answer —
    ``payload.answer = ""`` would render as "the owner answered:" with nothing
    after it in the retry prompt."""
    import surfaces.telegram.harness as h
    from agents.task.goals.board import GoalBoard

    monkeypatch.setattr(h, "_is_admin_owner", lambda _uid: True)
    board, goal, ask = _board_with_blocked_goal(str(tmp_path))

    out = await h._handle_owner_admin(
        _Agent(str(tmp_path)), _Result(f"/fulfill {ask.id}", "rob"), "/fulfill")
    assert out == "✅ Ask fulfilled — 1 goal(s) unblocked."
    reread = GoalBoard(os.path.join(str(tmp_path), "goals.db"))
    assert "answer" not in (reread.get(ask.id).payload or {})


def test_the_usage_line_documents_the_answer():
    """An argument nothing tells the owner about is an argument that does not
    exist — `/help fulfill` is the only place this seat can say so."""
    assert "answer" in _USAGE["/fulfill"] or "tell" in _USAGE["/fulfill"]
    assert "/fulfill" in _help_for("/fulfill")
