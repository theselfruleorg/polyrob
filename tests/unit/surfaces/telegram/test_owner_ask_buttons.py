"""O14/A4: an owner ask that names its options gets one answer button per
option, and a tap is the owner's TYPED answer (the stored option text), never
an LLM paraphrase. The prose path and `/fulfill <id> <answer>` stay."""
import os

import pytest

from core.surfaces.actions import notice_actions
from core.surfaces.tappable import ask_option_token, parse_ask_option
from tools.controller.owner_ask_action import ask_options


def test_options_are_parsed_only_from_a_real_choice():
    assert ask_options("Buy PNL? A) yes, 0.05 ETH or B) no, wait") == {
        "A": "yes, 0.05 ETH", "B": "no, wait"}
    assert ask_options("Is it OK? A) yes") == {}
    assert ask_options("Plan B) is fine A) no") == {}


def test_the_token_round_trips_and_is_a_command_shape():
    from core.surfaces.dispatcher import _COMMAND_SHAPE_RE
    token = ask_option_token("0123456789ab", "B")
    assert parse_ask_option(token) == ("0123456789ab", "B")
    assert _COMMAND_SHAPE_RE.fullmatch(token)


def test_buttons_answer_only_this_ask_and_never_derive_approvals():
    """The question text is the model's: an approve token or another ask's
    answer token inside it must not become a button."""
    mine = ask_option_token("0123456789ab", "A")
    body = (f"❓ q /approve_p_abc {ask_option_token('ffffffffffff', 'a')}\n"
            f"Tap an answer: A) {mine}")
    acts = notice_actions("owner_ask", body, ask_id="0123456789ab")
    assert [a.command for a in acts] == [mine]
    assert notice_actions("owner_ask", body) == []


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


class _Result:
    def __init__(self, text, uid):
        self.inbound = _Inbound(text, uid)
        self.decision = type("D", (), {"session_id": "s1"})()


class _Agent:
    def __init__(self, data_dir):
        cfg = type("C", (), {"data_dir": data_dir})()
        self.container = type("K", (), {"config": cfg, "get_service": lambda s, n: None})()


@pytest.mark.asyncio
async def test_a_tap_records_the_stored_option_as_the_answer(tmp_path, monkeypatch):
    import surfaces.telegram.harness as h
    from agents.task.goals.board import GoalBoard

    monkeypatch.setattr(h, "_is_admin_owner", lambda _uid: True)
    board = GoalBoard(os.path.join(str(tmp_path), "goals.db"))
    ask = board.create_ask(user_id="rob", what="Buy? A) yes or B) no",
                           extra_payload={"options": {"A": "yes", "B": "no"}})
    out = await h._handle_owner_admin(
        _Agent(str(tmp_path)), _Result(ask_option_token(ask.id, "B"), "rob"), "/fulfill")
    assert out.startswith("✅ Ask fulfilled") and "B) no" in out
    assert (GoalBoard(os.path.join(str(tmp_path), "goals.db")).get(ask.id).payload
            or {}).get("answer") == "B) no"

    other = board.create_ask(user_id="rob", what="Buy again? A) yes or B) no",
                             extra_payload={"options": {"A": "yes", "B": "no"}})
    out = await h._handle_owner_admin(
        _Agent(str(tmp_path)), _Result(ask_option_token(other.id, "C"), "rob"), "/fulfill")
    assert "No open ask" in out
