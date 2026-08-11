"""AuthStore file discipline (proposal 024 §4.1 / §8 L1)."""
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from core.llm_auth.store import AuthStore, AuthStoreError, auth_store_path


@pytest.fixture
def store(tmp_path):
    return AuthStore(tmp_path / "auth.json")


class TestFileDiscipline:
    def test_created_0600(self, store):
        store.set_provider("p", {"access_token": "t"})
        mode = stat.S_IMODE(os.stat(store.path).st_mode)
        assert mode == 0o600

    def test_atomic_replace_keeps_0600(self, store):
        store.set_provider("p", {"access_token": "t"})
        store.set_provider("q", {"access_token": "u"})
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600

    def test_load_missing_file_is_empty(self, store):
        data = store.load()
        assert data["providers"] == {} and data["borrowed"] == {}
        assert not store.path.exists()  # a pure read never creates the file

    def test_corrupt_file_treated_as_empty(self, store):
        store.path.write_text("{not json")
        assert store.load()["providers"] == {}

    def test_crash_mid_write_leaves_old_content(self, store, monkeypatch):
        store.set_provider("p", {"access_token": "t1"})

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("core.llm_auth.store.AuthStore._write_atomic", boom)
        with pytest.raises(OSError):
            store.set_provider("p", {"access_token": "t2"})
        monkeypatch.undo()
        assert store.get_provider("p")["access_token"] == "t1"

    def test_lock_contention_times_out(self, store, monkeypatch):
        import fcntl
        store.set_provider("p", {"access_token": "t"})
        monkeypatch.setattr("core.llm_auth.store._LOCK_TIMEOUT_SEC", 0.3)
        holder = open(store.path, "r+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        try:
            with pytest.raises(AuthStoreError, match="lock timeout"):
                store._mutate(lambda d: None)
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()

    def test_mutate_retries_when_inode_replaced_under_lock(self, store, monkeypatch):
        """flock+rename race: a lock acquired on an inode that a concurrent
        writer already replaced must be detected and retried, not trusted."""
        store.set_provider("p", {"access_token": "t1"})
        real_flock = AuthStore._flock
        raced = {"done": False}

        def racy_flock(self_store, fh, timeout=15.0):
            real_flock(self_store, fh, timeout)
            if not raced["done"]:
                raced["done"] = True
                # Simulate a concurrent writer swapping the file AFTER our
                # open+lock: the fd we locked is now a dead inode.
                self_store._write_atomic(self_store._read_unlocked())

        monkeypatch.setattr(AuthStore, "_flock", racy_flock)
        store.set_provider("q", {"access_token": "t2"})
        assert set(store.list_providers()) == {"p", "q"}

    def test_concurrent_writers_both_land(self, store):
        def write(name):
            store.set_provider(name, {"access_token": name})

        threads = [threading.Thread(target=write, args=(f"p{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert set(store.list_providers()) == {f"p{i}" for i in range(8)}


class TestHealthReconcile:
    def test_stamp_applies_and_newer_wins(self, store):
        store.set_provider("p", {"access_token": "t"})
        # connect seeds health at wall-clock now — later observations are newer
        t0 = time.time() + 10.0
        assert store.stamp_health("p", "exhausted", retry_after=100.0, at=t0)
        assert store.get_provider("p")["health"]["state"] == "exhausted"
        # an OLDER observation must not resurrect the credential
        assert store.stamp_health("p", "ok", at=t0 - 1.0) is False
        assert store.get_provider("p")["health"]["state"] == "exhausted"
        # a NEWER one wins
        assert store.stamp_health("p", "ok", at=t0 + 1.0) is True
        assert store.get_provider("p")["health"]["state"] == "ok"

    def test_stamp_unknown_provider_noop(self, store):
        assert store.stamp_health("ghost", "exhausted") is False


class TestBorrowed:
    def test_consent_required(self, store):
        with pytest.raises(AuthStoreError, match="consent"):
            store.set_borrowed("codex", {"access_token": "t", "consented": False})
        store.set_borrowed("codex", {"access_token": "t", "consented": True,
                                     "source": "codex_cli"})
        assert store.get_borrowed("codex")["source"] == "codex_cli"


class TestPathResolution:
    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("POLYROB_AUTH_STORE", str(tmp_path / "x.json"))
        assert auth_store_path() == tmp_path / "x.json"

    def test_default_under_polyrob_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("POLYROB_AUTH_STORE", raising=False)
        monkeypatch.setenv("POLYROB_HOME", str(tmp_path))
        assert auth_store_path() == tmp_path / "auth.json"


class TestHealthStateValidation:
    def test_invalid_state_never_written(self, store):
        """Review M2: an invalid state string would be mapped back to 'ok' by
        effective_state — a typo must not silently HEAL an exhausted credential."""
        store.set_provider("p", {"access_token": "t"})
        t0 = time.time() + 10.0
        assert store.stamp_health("p", "exhausted", at=t0) is True
        assert store.stamp_health("p", "exausted", at=t0 + 1.0) is False  # typo
        assert store.get_provider("p")["health"]["state"] == "exhausted"


class TestWriteFailureHygiene:
    def test_failed_write_leaves_no_temp_file(self, store, monkeypatch):
        """Review I5: a failed atomic write must not strand a temp file holding
        the full token set next to the store."""
        store.set_provider("p", {"access_token": "t"})
        real_write = os.write

        def boom(fd, payload):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("core.llm_auth.store.os.write", boom)
        with pytest.raises(OSError):
            store.set_provider("q", {"access_token": "u"})
        monkeypatch.undo()
        leftovers = [p for p in store.path.parent.iterdir()
                     if p.name.startswith(".auth.json.")]
        assert leftovers == []
        assert store.get_provider("p")["access_token"] == "t"  # old content intact
