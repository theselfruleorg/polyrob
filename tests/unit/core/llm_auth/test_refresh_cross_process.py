"""Two processes must not post the same single-use refresh token.

The in-process lock serializes threads; the per-provider refresh lock file serializes
processes. Here two ``AuthStore`` objects on one file stand in for two processes: the
helper thread takes the file lock through its own store (never the in-process lock).
"""
import functools
import os
import threading
import time

from core.llm_auth.flows.refresh import refresh_token
from core.llm_auth.store import AuthStore
from modules.llm.provider_spec import OAuthSpec

_SPEC = OAuthSpec(auth_url="https://x.test/d", token_url="https://x.test/t",
                  client_id="c", grant="device_code")


def _expired(store, name="myplan"):
    store.set_provider(name, {"access_token": "old", "refresh_token": "r-single-use",
                              "expires_at": time.time() - 1})


def test_the_second_process_finds_the_new_token_and_never_posts(tmp_path):
    path = tmp_path / "auth.json"
    other, mine = AuthStore(path), AuthStore(path)
    _expired(other)
    held, posts = threading.Event(), []

    def other_process_refreshes():
        with other.refresh_lock("myplan"):
            held.set()
            time.sleep(0.3)  # its POST
            other.set_provider("myplan", {"access_token": "NEW", "refresh_token": "r2",
                                          "expires_at": time.time() + 3600})

    t = threading.Thread(target=other_process_refreshes)
    t.start()
    assert held.wait(5)

    def post(url, form):
        posts.append(form["refresh_token"])
        return {"access_token": "second", "expires_in": 3600}

    assert refresh_token("myplan", _SPEC, store=mine, http_post=post) is None
    t.join()
    assert posts == [], "the used refresh token was posted a second time"
    assert mine.get_provider("myplan")["access_token"] == "NEW"


def test_a_refresh_lock_timeout_returns_none_with_no_post(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    other, mine = AuthStore(path), AuthStore(path)
    _expired(other)
    monkeypatch.setattr(mine, "refresh_lock",
                        functools.partial(mine.refresh_lock, timeout=0.2))
    held, release = threading.Event(), threading.Event()

    def hold():
        with other.refresh_lock("myplan"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(5)
    posts = []
    try:
        assert refresh_token("myplan", _SPEC, store=mine,
                             http_post=lambda u, f: posts.append(f) or {}) is None
    finally:
        release.set()
        t.join()
    assert posts == []


def test_the_refresh_lock_file_is_private_and_per_provider(tmp_path):
    store = AuthStore(tmp_path / "auth.json")
    with store.refresh_lock("myplan"):
        with store.refresh_lock("other"):  # another provider does not wait
            pass
    lock = tmp_path / "auth.json.refresh-myplan.lock"
    assert lock.exists()
    assert os.stat(lock).st_mode & 0o077 == 0
