"""095 W1c — a config line never destroys: ``set_line``/``with_override`` and the
operator line ``core.setting_proposals`` prints (SC-2), the pinned-value and
bundle-switch card rules (SC-15, X-12), and the ``/model <name> default`` card
(SC-16)."""
import pytest

from core.config_policy.members import OVERRIDES_ENV, set_line, with_override
from core.setting_proposals import (BUNDLE_RISK, CARD, CONFIG_RISK, MODEL_RISK, OPERATOR,
                                    REFUSED, classify)


# --- the primitives ------------------------------------------------------------

def test_set_line_member_and_plain_flag():
    assert set_line("MCP_ENABLED", True) == f"{OVERRIDES_ENV}=MCP_ENABLED=true"
    assert set_line("CODE_EXEC_BACKEND", "docker") == "CODE_EXEC_BACKEND=docker"


def test_with_override_keeps_every_other_entry_in_order():
    raw = "CRON_ENABLED=true, MCP_ENABLED=false,SELF_WAKE_ENABLED=true"
    assert with_override(raw, "mcp_enabled", "yes") == \
        "CRON_ENABLED=true,SELF_WAKE_ENABLED=true,MCP_ENABLED=true"
    assert with_override(raw, "MCP_ENABLED", None) == \
        "CRON_ENABLED=true,SELF_WAKE_ENABLED=true"
    assert with_override("", "MCP_ENABLED", "0") == "MCP_ENABLED=false"
    assert with_override(None, "MCP_ENABLED", True) == "MCP_ENABLED=true"


# --- SC-2: the printed operator line merges --------------------------------------

def test_a_member_operator_line_merges_into_the_running_overrides(monkeypatch):
    monkeypatch.setenv(OVERRIDES_ENV, "CRON_ENABLED=true,SELF_WAKE_ENABLED=false")
    p = classify("/config set MCP_ENABLED true")
    assert p.outcome == OPERATOR, p.reason
    assert p.env_line == (f"{OVERRIDES_ENV}=CRON_ENABLED=true,SELF_WAKE_ENABLED=false,"
                          "MCP_ENABLED=true")
    assert p.setting == "MCP_ENABLED=true"


def test_a_pasted_overrides_line_never_drops_an_existing_member(monkeypatch):
    monkeypatch.setenv(OVERRIDES_ENV, "CRON_ENABLED=true,SELF_WAKE_ENABLED=false")
    p = classify(f"/config set {OVERRIDES_ENV} MCP_ENABLED=true")
    assert p.outcome == REFUSED and not p.env_line
    assert "CRON_ENABLED" in p.reason and "SELF_WAKE_ENABLED" in p.reason
    assert (f"{OVERRIDES_ENV}=CRON_ENABLED=true,SELF_WAKE_ENABLED=false,MCP_ENABLED=true"
            in p.reason)


def test_a_pasted_overrides_line_that_keeps_every_member_is_filed(monkeypatch):
    monkeypatch.setenv(OVERRIDES_ENV, "CRON_ENABLED=true")
    p = classify(f"/config set {OVERRIDES_ENV} CRON_ENABLED=true,MCP_ENABLED=true")
    assert p.outcome == OPERATOR
    assert p.env_line == f"{OVERRIDES_ENV}=CRON_ENABLED=true,MCP_ENABLED=true"


def test_operator_asks_dedupe_by_setting_not_by_line(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import agents.task.agent.service  # noqa: F401 — import cycle guard
    from tools.controller import setting_actions as sa

    class _Board:
        rows = []

        def asks(self, *, user_id, status):
            return [a for a in self.rows if a.status == status]

        def create_ask(self, *, user_id, what, why="", extra_payload=None, force=False, **kw):
            a = SimpleNamespace(id=f"ask{len(self.rows):09d}", title=what, status="open",
                                payload=dict(extra_payload or {}))
            self.rows.append(a)
            return a

    board = _Board()
    monkeypatch.setattr("tools.controller.owner_ask_action._board", lambda c: board)
    monkeypatch.setenv(OVERRIDES_ENV, "CRON_ENABLED=true")
    asyncio.run(sa._file_for_operator(SimpleNamespace(), "alice", "s1",
                                      classify("/config set MCP_ENABLED true"), "why"))
    # After a restart the running line differs, so the merged line differs too.
    monkeypatch.setenv(OVERRIDES_ENV, "CRON_ENABLED=false")
    asyncio.run(sa._file_for_operator(SimpleNamespace(), "alice", "s1",
                                      classify("/config set MCP_ENABLED true"), "why"))
    assert len(board.rows) == 1
    asyncio.run(sa._file_for_operator(SimpleNamespace(), "alice", "s1",
                                      classify("/config set MCP_ENABLED false"), "why"))
    assert len(board.rows) == 2


# --- SC-15 / X-12: a pinned value is no card; a bundle switch says so --------------

def test_a_value_pinned_above_the_env_file_is_an_operator_line(monkeypatch):
    from core import config_service
    monkeypatch.setattr(config_service, "shadowed_write_refusal",
                        lambda k, v: f"nothing written: `{k}` is set in /etc/x.env")
    p = classify("/config set AUTONOMY_ENABLED true")
    assert p.outcome == OPERATOR and "/etc/x.env" in p.change


def test_autonomy_enabled_card_carries_the_bundle_risk(monkeypatch):
    from core import config_service
    monkeypatch.setattr(config_service, "shadowed_write_refusal", lambda k, v: None)
    p = classify("/config set AUTONOMY_ENABLED true")
    assert p.outcome == CARD and p.risk == BUNDLE_RISK
    other = classify("/config set AUTONOMY_HALT 1")
    assert other.outcome == CARD and other.risk == CONFIG_RISK


def test_autonomy_posture_is_never_a_generic_card():
    p = classify("/config set AUTONOMY_POSTURE full")
    assert not (p.outcome == CARD and p.risk == CONFIG_RISK)


# --- SC-16: the owner's new-chat model is a card ----------------------------------

def test_model_default_is_a_card_and_other_model_forms_are_refused():
    p = classify("/model openrouter/anthropic/claude-sonnet-5.5 default")
    assert p.outcome == CARD and p.risk == MODEL_RISK
    assert p.words == ["/model", "openrouter/anthropic/claude-sonnet-5.5", "default"]
    assert "DEFAULT_MODEL" in p.risk
    for line in ("/model sonnet", "/model sonnet go", "/model reset default",
                 "/model sonnet default go"):
        assert classify(line).outcome == REFUSED, line


def test_the_model_card_holds_exactly_the_default_line():
    from core.surfaces import cards
    with pytest.raises(ValueError):
        cards.setting_card("alice", ["/model", "sonnet"], title="x")
    with pytest.raises(ValueError):
        cards.setting_card("alice", ["/model", "sonnet", "default", "go"], title="x")
