from core.surfaces.session_chat_registry import build_session_key
from core.surfaces.envelopes import SessionSource


def test_dm_is_user_isolated():
    s = SessionSource(surface_id="telegram", chat_id="555", chat_type="dm")
    assert build_session_key(s, user_id="u_abc") == "agent:main:telegram:dm:555:u_abc"


def test_group_is_chat_shared_not_user_scoped():
    s = SessionSource(surface_id="telegram", chat_id="999", chat_type="group")
    k1 = build_session_key(s, user_id="u_abc")
    k2 = build_session_key(s, user_id="u_xyz")
    assert k1 == k2 == "agent:main:telegram:group:999"


def test_thread_appends_segment():
    s = SessionSource(surface_id="telegram", chat_id="999", chat_type="group", thread_id="7")
    assert build_session_key(s, user_id="u_abc") == "agent:main:telegram:group:999:thread:7"


def test_pure_no_side_effects():
    s = SessionSource(surface_id="cli", chat_id="local", chat_type="dm")
    assert build_session_key(s, "u1") == build_session_key(s, "u1")
