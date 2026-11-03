"""098 CO-20: an owner approval binds to the CONTENT the owner saw.

A restage after review is refused, a missing staging tree never removes the
served site, and stage/approve on one slug are serialised by the slug lock."""
import os
import shutil
import threading

import pytest

from core.publish import PublishStore, STATUS_LIVE, STATUS_PENDING


@pytest.fixture
def store(tmp_path):
    return PublishStore(db_path=str(tmp_path / "publications.db"),
                        root=str(tmp_path / "publish"),
                        base_url="https://pub.example.com")


def _src(tmp_path, text, name="index.html"):
    d = tmp_path / "ws"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text(text)
    return str(f)


def _served(store, slug):
    with open(os.path.join(store.dir_for(slug), "index.html")) as fh:
        return fh.read()


def test_stage_records_the_content_digest(store, tmp_path):
    pub = store.stage("alice", "promo", [_src(tmp_path, "v1")])
    assert len(pub.content_digest) == 64


def test_approve_refuses_restaged_content(store, tmp_path):
    reviewed = store.stage("alice", "promo", [_src(tmp_path, "v1")]).content_digest
    store.stage("alice", "promo", [_src(tmp_path, "v2-swapped")])
    assert store.approve("alice", "promo", expected_digest=reviewed) is False
    assert store.get("promo").status == STATUS_PENDING
    assert not os.path.isdir(store.dir_for("promo"))
    # The new content can be approved on its own digest.
    assert store.approve("alice", "promo") is True
    assert _served(store, "promo") == "v2-swapped"


def test_approve_refuses_a_tree_changed_on_disk(store, tmp_path):
    store.stage("alice", "promo", [_src(tmp_path, "v1")])
    with open(os.path.join(store.staging_dir_for("promo"), "index.html"), "w") as fh:
        fh.write("tampered")
    assert store.approve("alice", "promo") is False
    assert store.get("promo").status == STATUS_PENDING


def test_approve_without_staging_dir_refuses_and_keeps_served(store, tmp_path):
    store.stage("alice", "promo", [_src(tmp_path, "v1")])
    # A served tree exists (e.g. from an earlier life of the slug).
    os.makedirs(store.dir_for("promo"))
    with open(os.path.join(store.dir_for("promo"), "index.html"), "w") as fh:
        fh.write("old live page")
    shutil.rmtree(store.staging_dir_for("promo"))
    assert store.approve("alice", "promo") is False
    assert store.get("promo").status == STATUS_PENDING
    assert _served(store, "promo") == "old live page"


def test_approve_with_matching_digest_goes_live(store, tmp_path):
    pub = store.stage("alice", "promo", [_src(tmp_path, "v1")])
    assert store.approve("alice", "promo", expected_digest=pub.content_digest) is True
    assert store.get("promo").status == STATUS_LIVE
    assert _served(store, "promo") == "v1"


def test_stage_during_approve_is_serialised(store, tmp_path, monkeypatch):
    """A restage that arrives while approve holds the slug lock waits; it then
    writes into the SERVED tree of the now-live slug, never a lost staging."""
    import core.ship_tree as st
    reviewed = store.stage("alice", "promo", [_src(tmp_path, "v1")]).content_digest
    src2 = _src(tmp_path, "v2", name="v2.html")
    orig = st.tree_digest
    box = {}

    def racing_digest(root):
        if "t" not in box:
            def _restage():
                os.replace(src2, os.path.join(os.path.dirname(src2), "index.html"))
                store.stage("alice", "promo", [os.path.join(os.path.dirname(src2),
                                                             "index.html")])
            box["t"] = threading.Thread(target=_restage)
            box["t"].start()
            box["t"].join(0.3)
            box["blocked"] = box["t"].is_alive()
        return orig(root)

    monkeypatch.setattr(st, "tree_digest", racing_digest)
    assert store.approve("alice", "promo", expected_digest=reviewed) is True
    box["t"].join(5)
    assert box["blocked"] is True
    # The restage ran after the approval: it updated the live slug in place.
    assert store.get("promo").status == STATUS_LIVE
    assert _served(store, "promo") == "v2"


def test_concurrent_stage_and_approve_threads(store, tmp_path):
    """The served tree always equals the digest the row records as approved."""
    from core.ship_tree import tree_digest
    first = store.stage("alice", "promo", [_src(tmp_path, "v0")]).content_digest
    stop = threading.Event()

    def restager():
        i = 0
        while not stop.is_set():
            i += 1
            d = tmp_path / f"ws{i}"
            d.mkdir()
            (d / "index.html").write_text(f"v{i}")
            try:
                store.stage("alice", "promo", [str(d / "index.html")])
            except Exception:
                pass

    t = threading.Thread(target=restager)
    t.start()
    try:
        ok = store.approve("alice", "promo", expected_digest=first)
    finally:
        stop.set()
        t.join(10)
    pub = store.get("promo")
    if ok:
        assert pub.status == STATUS_LIVE
    else:
        assert pub.status == STATUS_PENDING
        assert not os.path.isdir(store.dir_for("promo"))
    if pub.status == STATUS_LIVE:
        assert tree_digest(store.dir_for("promo")) == pub.content_digest
