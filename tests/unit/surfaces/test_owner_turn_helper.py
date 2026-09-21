"""057 WS-H item 2: one owner-turn gate, reachable from every seat.

A deploy landed at step 8 of a live owner turn (and at step 4 of another at
10:36Z on 2026-09-19) because only the Telegram harness opened
`core.interactive_gate.owner_turn`. These cases pin the helper's contract, the
email surface's coverage through the shared executor, and the console/API run.
"""
import asyncio
import json
import os
import pathlib

import pytest

from core.surfaces.owner_turn import FLAG, surface_owner_turn

REPO = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    h = tmp_path / "data"
    h.mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(h))
    return h


def _marker(home):
    p = home / "locks" / "turn.active"
    return json.loads(p.read_text()) if p.exists() else None


def test_marker_is_written_for_the_body_and_cleared_after(data_home):
    with surface_owner_turn(kind="console_chat", session_id="s1") as held:
        assert held is True
        m = _marker(data_home)
        assert m and m["kind"] == "console_chat" and m["session_id"] == "s1"
        assert m["pid"] == os.getpid()
    assert _marker(data_home) is None


def test_the_process_is_marked_busy_only_inside(data_home):
    from core.interactive_gate import is_interactive_busy
    assert not is_interactive_busy()
    with surface_owner_turn():
        assert is_interactive_busy()
    assert not is_interactive_busy()


def test_an_exception_in_the_body_still_clears_the_marker(data_home):
    with pytest.raises(ValueError):
        with surface_owner_turn(kind="console_chat"):
            raise ValueError("turn blew up")
    assert _marker(data_home) is None


def test_the_flag_reverts_to_the_pre_057_behaviour(data_home, monkeypatch):
    monkeypatch.setenv(FLAG, "false")
    with surface_owner_turn(kind="console_chat") as held:
        assert held is False
        assert _marker(data_home) is None


def test_a_gate_fault_never_costs_the_human_their_turn(data_home, monkeypatch):
    import core.interactive_gate as ig

    def boom(**kw):
        raise RuntimeError("no data home")
    monkeypatch.setattr(ig, "owner_turn", boom)
    ran = False
    with surface_owner_turn(kind="console_chat") as held:
        assert held is False
        ran = True
    assert ran


def test_the_console_api_run_holds_the_gate():
    """The wiring is the point: a helper nothing calls marks nothing."""
    src = (REPO / "api/task_http_api.py").read_text()
    assert "from core.surfaces.owner_turn import surface_owner_turn" in src
    assert 'with surface_owner_turn(kind="console_chat"' in src
    assert "await agent.run_session(_user_id, _session_id)" in src


def test_the_email_surface_is_covered_by_the_shared_executor():
    """Email routes through `act_on_inbound` -> `_run_and_deliver`, which
    already opens `owner_turn`. Pinned so a refactor of the shared executor
    cannot silently drop the email seat's coverage."""
    email = (REPO / "surfaces/email/harness.py").read_text()
    assert "from surfaces.telegram.harness import act_on_inbound" in email
    tg = (REPO / "surfaces/telegram/harness.py").read_text()
    assert "from core.interactive_gate import owner_turn" in tg
    assert "with owner_turn(kind=_kind, session_id=session_id):" in tg
    # ...and the executor email calls really is the one that opens it.
    assert "_spawn(_run_and_deliver(task_agent" in tg


def test_nested_turns_clear_the_marker_only_once(data_home):
    async def inner():
        with surface_owner_turn(kind="console_chat", session_id="a"):
            with surface_owner_turn(kind="console_chat", session_id="a"):
                assert _marker(data_home) is not None
            assert _marker(data_home) is not None, "the outer turn is still live"
        assert _marker(data_home) is None
    asyncio.run(inner())
