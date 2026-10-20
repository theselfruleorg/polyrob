"""081 WS-3.1 — the owner chat seat runs on the cron shape: a narrow eager rig,
the <tool-catalog> and load_tool, the tool_call bridge, and ONE wording.

The owner's condition (2026-10-15): "an agent should understand that it needs
more tools sometimes and have a way to get about it. we need coherent and aligned
user experience for either of the way". So: an owner seat always has the catalog
(no flag), it reaches a late tool the way a cron run does, the texts are the same
on every seat, and load_tool never reaches a money tool.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agents.task.session_class import stamp_owner_seat, tool_disclosure_enabled
from core import autonomy_marker as am


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("TOOL_PROGRESSIVE_DISCLOSURE", "AUTONOMOUS_TOOL_DISCLOSURE", "POLYROB_LOCAL",
              "TOOL_SCHEMAS_FROZEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")


def _orch(sid, user="rob", public=False, correspondent=False):
    return SimpleNamespace(session_id=sid, user_id=user, _public_session=public,
                           _correspondent_session=correspondent)


# --- the seat ------------------------------------------------------------------

def test_the_owner_principal_chat_is_a_seat_with_the_catalog_and_no_flag():
    assert stamp_owner_seat(_orch("seat-owner")) is True
    assert am.is_owner_seat("seat-owner")
    # TOOL_PROGRESSIVE_DISCLOSURE is off here: the seat still pins the catalog.
    assert tool_disclosure_enabled("seat-owner") is True


@pytest.mark.parametrize("sid,orch", [
    ("seat-tenant", _orch("seat-tenant", user="usr_tenant")),
    ("seat-room", _orch("seat-room", public=True)),
    ("seat-corr", _orch("seat-corr", correspondent=True)),
])
def test_a_tenant_a_room_and_a_correspondent_are_never_a_seat(sid, orch):
    assert stamp_owner_seat(orch) is False
    assert not am.is_owner_seat(sid)
    assert tool_disclosure_enabled(sid) is False


def test_an_autonomous_run_is_never_a_seat():
    am.mark_autonomous("seat-cron", cron_job_id="job-1")
    assert stamp_owner_seat(_orch("seat-cron")) is False


def test_an_owner_seat_takes_the_bridge_like_a_cron_run():
    from tools.late_tool_mode import disclosure_bridge, session_late_tool_mode
    stamp_owner_seat(_orch("seat-bridge"))
    assert disclosure_bridge("seat-bridge") is True
    assert session_late_tool_mode("openrouter", "seat-bridge") == "bridge"
    assert disclosure_bridge("never-stamped") is False


# --- one wording on every seat -------------------------------------------------

class _Container:
    def __init__(self, services):
        self._s = set(services)

    def has_service(self, name):
        return name in self._s


class _Controller:
    def __init__(self, sid, services=(), loaded=()):
        self.session_id = sid
        self.container = _Container(services)
        self._loaded = list(loaded)
        self.load_calls = []
        self.registry = SimpleNamespace(schemas_frozen=lambda: True)

    def list_tools(self):
        return list(self._loaded)

    async def load_tools_from_container(self, ids):
        self.load_calls.append(list(ids))
        self._loaded += ids
        return {i: SimpleNamespace(actions=("a", "b")) for i in ids}


def test_the_load_result_and_the_refusal_are_the_cron_texts():
    from tools.tool_disclosure import load_result_text, not_loaded_text, perform_load_tool
    stamp_owner_seat(_orch("seat-words"))
    am.mark_autonomous("cron-words", cron_job_id="job-w")
    for sid in ("seat-words", "cron-words"):
        c = _Controller(sid, services={"email"})
        res = asyncio.run(perform_load_tool(c, "email"))
        # The truthful line the agent can quote to the owner: it loaded the tool.
        assert res.extracted_content == load_result_text("email", 2, True)
        assert "Tool 'email' loaded" in res.extracted_content
        assert "tool_call(" in res.extracted_content
        fresh = _Controller(sid, services={"email"})
        text = not_loaded_text("email_send", fresh)
        assert "belongs to the tool 'email'" in text and 'load_tool("email")' in text


def test_the_catalog_says_the_agent_holds_part_of_it_and_names_load_tool():
    from tools.tool_disclosure import CATALOG_INTRO, render_tool_catalog
    text = render_tool_catalog(container=_Container({"email"}), loaded_ids={"filesystem"})
    assert CATALOG_INTRO in text
    assert "the catalog lists more" in CATALOG_INTRO
    assert 'load_tool("<id>")' in CATALOG_INTRO


# --- money: load_tool never reaches it on the owner seat -----------------------

def test_load_tool_never_loads_a_money_tool_on_an_owner_seat():
    from core.tool_capabilities import ids_with
    from tools.tool_disclosure import perform_load_tool
    stamp_owner_seat(_orch("seat-money"))
    money = sorted(ids_with("money"))
    assert "defi_trade" in money
    c = _Controller("seat-money", services=set(money))
    for tool in money:
        res = asyncio.run(perform_load_tool(c, tool))
        assert "load_tool refused (gated:money)" in res.extracted_content, tool
    assert c.load_calls == []                   # nothing was ever loaded
    assert not set(money) & set(c.list_tools())


def test_a_call_to_an_unheld_money_action_names_the_owner_grant():
    from tools.tool_disclosure import not_loaded_text
    stamp_owner_seat(_orch("seat-money-call"))
    c = _Controller("seat-money-call", services={"defi_trade"})
    text = not_loaded_text("defi_trade_swap", c)
    assert "gated:money" in text
    assert "owner" in text and "load_tool never loads" in text
    assert 'load_tool("defi_trade")' not in text


def test_an_owner_seat_prompt_pins_the_catalog_without_polyrob_local():
    from agents.task.agent.prompts import SystemPrompt
    assert SystemPrompt("a", owner_seat=True)._catalog_pinned() is True
    assert SystemPrompt("a")._catalog_pinned() is False


def test_one_block_owns_tool_status_when_the_catalog_is_pinned():
    """081 bar 2: with the catalog pinned, <tool-availability> carries no tool line."""
    from agents.task.agent.core.tool_availability import build_tool_availability_note
    note = build_tool_availability_note(set(), catalog_pinned=True)
    assert "\n- " not in note and "Goals you create" in note
    assert build_tool_availability_note(set(), catalog_pinned=True, can_create_goals=False) == ""
