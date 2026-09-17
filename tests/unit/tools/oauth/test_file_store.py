"""T2.4 Task 1 — FileTokenStore: persisted MutableMapping backing OAuthManager."""
import json
import logging
import stat

import pytest

from tools.oauth.file_store import FileTokenStore, TOKENS_FILENAME


# --- MutableMapping contract --------------------------------------------------

def test_setitem_getitem_roundtrip(tmp_path):
    path = tmp_path / TOKENS_FILENAME
    store = FileTokenStore(path)
    store[("u1", "generic_oauth2")] = b"\x00\x01ciphertext\xff"
    assert store[("u1", "generic_oauth2")] == b"\x00\x01ciphertext\xff"


def test_contains_and_len(tmp_path):
    store = FileTokenStore(tmp_path / TOKENS_FILENAME)
    assert ("u1", "p") not in store
    assert len(store) == 0
    store[("u1", "p")] = b"abc"
    assert ("u1", "p") in store
    assert len(store) == 1


def test_delitem_removes_entry(tmp_path):
    store = FileTokenStore(tmp_path / TOKENS_FILENAME)
    store[("u1", "p")] = b"abc"
    del store[("u1", "p")]
    assert ("u1", "p") not in store
    assert len(store) == 0
    with pytest.raises(KeyError):
        del store[("u1", "p")]


def test_iter_and_get_default(tmp_path):
    store = FileTokenStore(tmp_path / TOKENS_FILENAME)
    store[("u1", "p1")] = b"a"
    store[("u2", "p2")] = b"b"
    assert set(iter(store)) == {("u1", "p1"), ("u2", "p2")}
    assert store.get(("nobody", "p1")) is None
    assert store.get(("nobody", "p1"), "default") == "default"


# --- persistence across instances ---------------------------------------------

def test_roundtrip_across_instances(tmp_path):
    path = tmp_path / TOKENS_FILENAME
    a = FileTokenStore(path)
    a[("user1", "mock")] = b"opaque-ciphertext-bytes"
    a[("user2", "generic_oauth2")] = b"more-bytes\x00\x01"

    b = FileTokenStore(path)  # fresh instance, same file
    assert b[("user1", "mock")] == b"opaque-ciphertext-bytes"
    assert b[("user2", "generic_oauth2")] == b"more-bytes\x00\x01"
    assert len(b) == 2


def test_delete_persists_across_instances(tmp_path):
    path = tmp_path / TOKENS_FILENAME
    a = FileTokenStore(path)
    a[("u", "p")] = b"x"
    del a[("u", "p")]

    b = FileTokenStore(path)
    assert ("u", "p") not in b
    assert len(b) == 0


# --- file mode -------------------------------------------------------------

def test_file_written_with_0600_mode(tmp_path):
    path = tmp_path / TOKENS_FILENAME
    store = FileTokenStore(path)
    store[("u", "p")] = b"secret-ish-bytes"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


# --- values are opaque base64 in the JSON file --------------------------------

def test_values_stored_as_base64_json_strings(tmp_path):
    path = tmp_path / TOKENS_FILENAME
    store = FileTokenStore(path)
    store[("u", "p")] = b"\xffnot-utf8\x00"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    (only_value,) = raw.values()
    assert isinstance(only_value, str)  # JSON-safe string, not raw bytes
    import base64
    assert base64.b64decode(only_value) == b"\xffnot-utf8\x00"


# --- key encoding / separator collision ----------------------------------------

def test_pipe_in_user_id_does_not_collide(tmp_path):
    """('a|b', 'c') and ('a', 'b|c') must serialize to DISTINCT keys."""
    path = tmp_path / TOKENS_FILENAME
    store = FileTokenStore(path)
    store[("a|b", "c")] = b"one"
    store[("a", "b|c")] = b"two"
    assert len(store) == 2
    assert store[("a|b", "c")] == b"one"
    assert store[("a", "b|c")] == b"two"

    # Round-trips through a fresh instance too.
    fresh = FileTokenStore(path)
    assert fresh[("a|b", "c")] == b"one"
    assert fresh[("a", "b|c")] == b"two"


# --- corrupt file: fail-open + loud warning ------------------------------------

def test_corrupt_file_fails_open_to_empty_with_warning(tmp_path, caplog):
    path = tmp_path / TOKENS_FILENAME
    path.write_text("{not valid json at all", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="tools.oauth.file_store"):
        store = FileTokenStore(path)
    assert len(store) == 0
    assert dict(store) == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "corrupt" in warnings[0].message.lower() or "unreadable" in warnings[0].message.lower()
    # the bad file is renamed aside (not left in place, not deleted) BEFORE the
    # empty store is returned, and the warning names the new path.
    assert not path.exists()
    corrupt_files = list(tmp_path.glob(f"{TOKENS_FILENAME}.corrupt-*"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].read_text(encoding="utf-8") == "{not valid json at all"
    assert str(corrupt_files[0]) in warnings[0].message


def test_corrupt_file_top_level_not_object_fails_open(tmp_path, caplog):
    path = tmp_path / TOKENS_FILENAME
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="tools.oauth.file_store"):
        store = FileTokenStore(path)
    assert len(store) == 0
    # same rename-aside contract applies to this corruption shape too
    assert not path.exists()
    assert list(tmp_path.glob(f"{TOKENS_FILENAME}.corrupt-*"))


def test_missing_file_is_empty_no_warning(tmp_path, caplog):
    path = tmp_path / "does-not-exist" / TOKENS_FILENAME
    with caplog.at_level(logging.WARNING, logger="tools.oauth.file_store"):
        store = FileTokenStore(path)
    assert len(store) == 0
    assert caplog.records == []


# --- default path resolution ---------------------------------------------------

def test_default_path_under_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    store = FileTokenStore()
    assert store.path == tmp_path / TOKENS_FILENAME


# --- integration: usable as OAuthManager's store --------------------------------

def test_usable_as_oauth_manager_store(tmp_path):
    from tools.mcp.security import MCPEncryption
    from tools.oauth import OAuthManager, OAuthToken

    path = tmp_path / TOKENS_FILENAME
    enc = MCPEncryption(key=MCPEncryption.generate_key())
    mgr = OAuthManager(encryption=enc, store=FileTokenStore(path))
    token = OAuthToken(access_token="abc123", refresh_token="r0")
    mgr.store_token("user1", "mock", token)

    # A second manager instance, pointed at the SAME file + key, sees the token.
    mgr2 = OAuthManager(encryption=enc, store=FileTokenStore(path))
    loaded = mgr2.load_token("user1", "mock")
    assert loaded is not None
    assert loaded.access_token == "abc123"


# --- two-instance clobber (the X session P0) -----------------------------------

def test_two_instances_same_file_do_not_clobber(tmp_path):
    """A delete via one instance must not erase a row another instance wrote.

    The X login store (provider "x") and the signup-progress store
    (provider "x_signup") are separate FileTokenStore objects over the SAME
    .x_session.json. Before the read-before-mutate fix, a delete flushed a stale
    whole-file snapshot and erased the just-saved login row.
    """
    path = tmp_path / TOKENS_FILENAME
    login = FileTokenStore(path)          # snapshot A (empty)
    progress = FileTokenStore(path)       # snapshot B (empty)

    progress[("u1", "x_signup")] = b"progress-blob"
    login[("u1", "x")] = b"login-blob"    # writes with A's stale snapshot

    # A THIRD instance (fresh disk read) must see BOTH rows.
    fresh = FileTokenStore(path)
    assert fresh[("u1", "x")] == b"login-blob"
    assert fresh[("u1", "x_signup")] == b"progress-blob"

    # Deleting progress via B must not erase the login row.
    progress[("u1", "x_signup")] = b"progress-blob2"  # B still has stale snapshot
    del progress[("u1", "x_signup")]
    fresh2 = FileTokenStore(path)
    assert fresh2[("u1", "x")] == b"login-blob"
    assert ("u1", "x_signup") not in fresh2


# --- unreadable is NOT corrupt: never rename another process's file -----------

def test_permission_denied_leaves_the_file_in_place(tmp_path, caplog, monkeypatch):
    """2026-09-17 prod: a non-root unit got EACCES on the root-owned store and the
    corruption branch renamed it aside, deleting the agent's just-imported X
    OAuth2 pair. A process that cannot READ a file has no standing to MOVE it."""
    import logging
    from pathlib import Path
    from tools.oauth.file_store import FileTokenStore
    path = tmp_path / "tokens.json"
    path.write_text('{"u|p": "aGk="}')

    real_read_text = Path.read_text

    def _denied(self, *a, **k):
        if self == path:
            raise PermissionError(13, "Permission denied", str(path))
        return real_read_text(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", _denied)

    with caplog.at_level(logging.WARNING):
        store = FileTokenStore(path)
        assert dict(store) == {}
    assert path.exists()                          # NOT renamed aside
    assert not list(tmp_path.glob("tokens.json.corrupt-*"))
    msgs = [r.getMessage().lower() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("not readable" in m and "untouched" in m for m in msgs)


# --- shared-data identity: group + mode follow the directory / the target -----

def test_new_file_in_group_writable_dir_is_0660_with_the_dir_group(tmp_path):
    """Prod convention: <writer>:polyrob-data 0660 under a 0770 data dir. A
    0600 file in the writer's primary group is unreadable by the sibling unit
    that needs it (the agent could not read the root-imported pair, 2026-09-17)."""
    d = tmp_path / "data"
    d.mkdir()
    d.chmod(0o770)  # umask-proof: the convention is a group-writable data dir
    path = d / TOKENS_FILENAME
    FileTokenStore(path)[("u", "p")] = b"x"
    st = path.stat()
    assert stat.S_IMODE(st.st_mode) == 0o660
    assert st.st_gid == d.stat().st_gid


def test_rewrite_preserves_the_existing_mode(tmp_path):
    path = tmp_path / TOKENS_FILENAME
    path.write_text("{}")
    path.chmod(0o660)
    FileTokenStore(path)[("u", "p")] = b"x"
    assert stat.S_IMODE(path.stat().st_mode) == 0o660
