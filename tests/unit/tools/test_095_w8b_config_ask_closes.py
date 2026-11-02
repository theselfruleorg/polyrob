"""095 W8b (SC-11): an operator ``config_change`` ask closes by itself — when its
setting is in effect, or when it expires. It used to stay open forever."""
import asyncio
from types import SimpleNamespace

import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from agents.task.goals.board import GoalBoard
from core.goal_vocab import ASK_KIND_CONFIG_CHANGE, ASK_OBSOLETE, ASK_OPEN
from core.setting_proposals import CONFIG_ASK_TTL_DAYS, setting_in_effect


class _Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def _board(tmp_path):
    clock = _Clock()
    return GoalBoard(str(tmp_path / "goals.db"), clock=clock), clock


def _ask(board, setting, **extra):
    payload = {"ask_kind": ASK_KIND_CONFIG_CHANGE, "line": setting, "setting": setting}
    payload.update(extra)
    return board.create_ask(user_id="alice", what=f"Operator: set {setting}",
                            extra_payload=payload, force=True)


def test_an_applied_setting_closes_its_ask_as_obsolete(tmp_path):
    board, _ = _board(tmp_path)
    a = _ask(board, "CODE_EXEC_BACKEND=docker")
    b = _ask(board, "CRON_ENABLED=true")
    n = board.close_settled_config_asks(in_effect=lambda s: s == "CODE_EXEC_BACKEND=docker")
    assert n == 1
    assert board.get(a.id).status == ASK_OBSOLETE
    assert "in effect" in board.get(a.id).payload["obsolete_reason"]
    assert board.get(b.id).status == ASK_OPEN


def test_an_ask_expires_and_an_unreadable_value_keeps_it_open_until_then(tmp_path):
    board, clock = _board(tmp_path)
    a = _ask(board, "CODE_EXEC_BACKEND=docker")
    b = _ask(board, "CRON_ENABLED=true", expires_at=clock.t + 3600)
    assert board.close_settled_config_asks(in_effect=lambda s: None) == 0
    clock.t += 7200
    assert board.close_settled_config_asks(in_effect=lambda s: None) == 1
    assert board.get(b.id).status == ASK_OBSOLETE
    assert board.get(a.id).status == ASK_OPEN
    clock.t += CONFIG_ASK_TTL_DAYS * 86400
    assert board.close_settled_config_asks(in_effect=lambda s: None) == 1
    assert board.get(a.id).payload["obsolete_reason"].startswith("expired")


def test_other_asks_are_never_touched(tmp_path):
    board, clock = _board(tmp_path)
    plain = board.create_ask(user_id="alice", what="Need the API key", force=True)
    clock.t += 365 * 86400
    assert board.close_settled_config_asks(in_effect=lambda s: True) == 0
    assert board.get(plain.id).status == ASK_OPEN


def test_setting_in_effect_reads_the_effective_value(monkeypatch):
    monkeypatch.setenv("CRON_ENABLED", "true")
    assert setting_in_effect("CRON_ENABLED=true") is True
    assert setting_in_effect("CRON_ENABLED=false") is False
    assert setting_in_effect("NOT_A_KEY_095=1") is None
    assert setting_in_effect("no-equals") is None


def test_an_overrides_line_holds_when_every_named_member_holds(monkeypatch):
    monkeypatch.setenv("POLYROB_OVERRIDES", "MCP_ENABLED=true")
    assert setting_in_effect("POLYROB_OVERRIDES=MCP_ENABLED=true") is True
    assert setting_in_effect("POLYROB_OVERRIDES=MCP_ENABLED=false") is False


def test_a_filed_ask_carries_its_expiry_and_says_it_closes_by_itself(monkeypatch, tmp_path):
    from core.setting_proposals import classify
    from tools.controller import setting_actions as sa
    board, clock = _board(tmp_path)
    monkeypatch.setattr("tools.controller.owner_ask_action._board", lambda c: board)
    p = classify("/config set CODE_EXEC_BACKEND docker")
    res = asyncio.run(sa._file_for_operator(SimpleNamespace(), "alice", "s1", p, "why"))
    assert res.error is None and "closes by itself" in res.extracted_content
    (ask,) = board.asks(user_id="alice", status=ASK_OPEN)
    assert ask.payload["expires_at"] == clock.t + CONFIG_ASK_TTL_DAYS * 86400
