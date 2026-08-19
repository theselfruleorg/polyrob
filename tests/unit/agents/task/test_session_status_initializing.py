"""2026-08-16: 'initializing' joins the SessionStatus enum.

orchestrator.__init__ has stamped status='initializing' since the warm-resume
work, but the enum never carried it — so every session (re)build logged
"Invalid status value 'initializing', using as-is" (247× in 4 days of prod
journal) and took the stringly-typed back-compat path.
"""
from agents.task.agent.session import SessionStatus, get_user_status


def test_initializing_is_a_valid_session_status():
    assert SessionStatus("initializing") is SessionStatus.INITIALIZING


def test_initializing_maps_to_active_user_status():
    assert get_user_status("initializing") == "active"
    assert get_user_status("INITIALIZING") == "active"
