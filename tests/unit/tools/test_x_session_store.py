"""XSessionStore — encrypted X login custody (Task 8, 2026-08-18 plan)."""
import json

import pytest

from tools.x_browser.session_store import XSessionStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    # Deterministic Fernet key so tests never touch the persisted dev key.
    from cryptography.fernet import Fernet
    monkeypatch.setenv("MCP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return XSessionStore(path=tmp_path / ".x_session.json")


STATE = {"cookies": [{"name": "auth_token", "value": "tok", "domain": ".x.com"}],
         "origins": []}


def test_round_trip(store):
    store.save("u1", storage_state=STATE, password="s3cret", handle="robbot")
    loaded = store.load("u1")
    assert loaded["storage_state"] == STATE
    assert loaded["password"] == "s3cret"
    assert loaded["handle"] == "robbot"
    assert loaded["created_at"]


def test_merge_keeps_unspecified_fields(store):
    store.save("u1", storage_state=STATE, password="s3cret", handle="robbot")
    store.save("u1", storage_state={"cookies": [], "origins": []})
    loaded = store.load("u1")
    assert loaded["password"] == "s3cret"
    assert loaded["handle"] == "robbot"
    assert loaded["storage_state"] == {"cookies": [], "origins": []}


def test_missing_is_none_and_exists(store):
    assert store.load("nobody") is None
    assert store.exists("nobody") is False
    store.save("u1", storage_state=STATE)
    assert store.exists("u1") is True


def test_delete(store):
    store.save("u1", storage_state=STATE, password="pw")
    store.delete("u1")
    assert store.load("u1") is None


def test_file_never_holds_plaintext(store, tmp_path):
    store.save("u1", storage_state=STATE, password="super-plain-secret")
    raw = (tmp_path / ".x_session.json").read_bytes()
    assert b"super-plain-secret" not in raw
    assert b"auth_token" not in raw


def test_tenants_are_isolated(store):
    store.save("u1", storage_state=STATE, password="pw1")
    store.save("u2", storage_state=STATE, password="pw2")
    assert store.load("u1")["password"] == "pw1"
    assert store.load("u2")["password"] == "pw2"
    store.delete("u1")
    assert store.load("u2") is not None
