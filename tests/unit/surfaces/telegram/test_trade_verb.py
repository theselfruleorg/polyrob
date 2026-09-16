"""`/trade <task>` — the owner launches a money-granted run in one message.

The owner, 2026-09-09: "I need a freaking easy model of trading without hitting
there is no defi trade tool here. I need agent to be able to freaking launch
defi trade session."

Today a money verb reaches a run ONLY through a stream leg in
``data/streams/streams.yaml``, which seeds on a 4-hourly timer. Anything the
agent creates for itself has money stripped by ``goal_create`` — correctly,
since an INJECTED goal must never trade — so an on-demand "bridge this" or
"trade that" produces a goal that looks fine and can never execute. That loop
has now run ~50 times.

The missing signal is that the OWNER asking, in an authenticated chat, IS the
authorization. This verb carries it. The agent still cannot self-grant: the
grant comes from an owner-authenticated command, and every spend the run
attempts is still bounded by the caps and still queues for approval above the
autonomous ceiling.
"""
import types

import pytest


class _Board:
    def __init__(self):
        self.created = []

    def create(self, **kw):
        self.created.append(kw)
        return types.SimpleNamespace(id="new0000000", title=kw.get("title", ""))

    def list(self, user_id=None, limit=1000):
        return []


def _reply(args, board=None):
    from surfaces.telegram import owner_ops
    return owner_ops.trade_reply("owner", "/tmp", args, board=board or _Board())


class TestItGrantsTheMoneyVerb:
    def test_the_created_goal_carries_defi_trade(self):
        board = _Board()
        _reply(["bridge", "0.93", "SOL", "to", "Base"], board=board)
        assert board.created, "the verb must actually seed a goal"
        tools = board.created[0]["payload"]["tools"]
        assert "defi_trade" in tools, (
            "the whole point: an owner-launched run carries the money verb")
        assert "defi_data" in tools, "it needs to read the market it trades in"

    def test_the_task_text_survives_into_the_goal(self):
        board = _Board()
        _reply(["bridge", "0.93", "SOL", "to", "Base"], board=board)
        blob = (board.created[0]["title"] + board.created[0]["body"]).lower()
        assert "bridge 0.93 sol to base" in blob

    def test_it_is_marked_owner_granted_for_audit(self):
        board = _Board()
        _reply(["sell", "everything"], board=board)
        assert board.created[0]["payload"].get("owner_granted") is True


class TestItIsStillBounded:
    def test_the_reply_states_that_spends_still_need_approval(self):
        """The owner must not think this bypasses the approval queue."""
        assert "approv" in _reply(["bridge", "sol"]).lower()

    def test_it_names_only_real_owner_actions(self):
        from core.owner_remedy import unknown_owner_actions
        assert unknown_owner_actions(_reply(["bridge", "sol"])) == []


class TestGuardrails:
    def test_an_empty_task_is_refused_with_usage(self):
        assert "usage" in _reply([]).lower()

    def test_a_non_owner_never_reaches_it(self):
        """Routing owner-gates the verb; this pins that the handler itself does
        not assume it. A money grant must not rest on one layer."""
        from surfaces.telegram import owner_ops
        out = owner_ops.trade_reply(None, "/tmp", ["bridge"], board=_Board())
        assert "owner" in out.lower()
