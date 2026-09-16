from core.surfaces.room_keys import is_group_session_key, owner_only_reply_target


def test_group_key_shapes():
    assert is_group_session_key("agent:main:telegram:group:-100123")
    assert is_group_session_key("agent:main:telegram:supergroup:-100123:thread:7")
    assert is_group_session_key("agent:main:telegram:channel:-100999")
    assert not is_group_session_key("agent:main:telegram:dm:555:u_abc")
    assert not is_group_session_key("direct:telegram:555")
    assert not is_group_session_key("")


def test_owner_only_target_redirects_group_to_owner_dm(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "4242")
    assert owner_only_reply_target("agent:main:telegram:group:-100123", "-100123") == "4242"
    assert owner_only_reply_target("agent:main:telegram:dm:555:u_abc", "555") == "555"


def test_owner_only_target_none_when_owner_unknown(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    assert owner_only_reply_target("agent:main:telegram:group:-100123", "-100123") is None
