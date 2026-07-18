"""Wave 3 Task 1 — group-chat ingress allowlist (default-DENY)."""
from core.surfaces.group_allowlist import GroupAllowlist


def _store(tmp_path):
    return GroupAllowlist(str(tmp_path / "group_allowlist.db"))


def test_default_deny(tmp_path):
    store = _store(tmp_path)
    assert store.is_allowed("discord", "123") is False


def test_allow_then_allowed(tmp_path):
    store = _store(tmp_path)
    store.allow("discord", "123", note="dev server #general")
    assert store.is_allowed("discord", "123") is True
    assert store.is_allowed("discord", "456") is False
    assert store.is_allowed("slack", "123") is False


def test_revoke(tmp_path):
    store = _store(tmp_path)
    store.allow("discord", "123")
    assert store.revoke("discord", "123") is True
    assert store.is_allowed("discord", "123") is False
    assert store.revoke("discord", "123") is False  # already revoked


def test_reallow_after_revoke(tmp_path):
    store = _store(tmp_path)
    store.allow("discord", "123")
    store.revoke("discord", "123")
    store.allow("discord", "123", note="back")
    assert store.is_allowed("discord", "123") is True


def test_list_all(tmp_path):
    store = _store(tmp_path)
    store.allow("discord", "1", note="a")
    store.allow("slack", "C42", note="b")
    rows = store.list_all()
    assert {(r["surface"], r["chat_id"]) for r in rows} == {
        ("discord", "1"), ("slack", "C42")}


def test_chat_id_coerced_to_str(tmp_path):
    store = _store(tmp_path)
    store.allow("discord", 999)
    assert store.is_allowed("discord", "999") is True
