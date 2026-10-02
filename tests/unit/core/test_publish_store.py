"""The ship rail's store: a slug, a served directory, and an owner decision.

Rob could build a page or an API and had no way to give anyone a URL. Every
"ship" goal therefore ended as a request to the owner — "Ship x402 ecosystem
map: static HTML visualization" completed with a file nobody could reach, and
"Owner deploy package: mainnet-ready x402 endpoint" blocked outright.

A publication is (slug -> directory) plus an owner decision. The slug is the
public identity, so it is validated hard: it lands in a URL and in a filesystem
path, and a slug that can traverse either is a hole.
"""
import os

import pytest

from core.publish import PublishStore, valid_slug


@pytest.fixture
def store(tmp_path):
    return PublishStore(db_path=str(tmp_path / "publications.db"),
                        root=str(tmp_path / "publish"),
                        base_url="https://pub.example.com")


@pytest.mark.parametrize("slug", ["rob-status", "x402-map", "a", "a1-b2", "x" * 48])
def test_good_slugs_are_accepted(slug):
    assert valid_slug(slug) is True


@pytest.mark.parametrize("slug", [
    "", "..", "../etc", "a/b", "a b", "UPPER", "-lead", "trail-",
    "under_score", "x" * 49, ".hidden", "a..b", "sl%2fash",
    "abc\n", "abc\n ", "\nabc",  # trailing/leading newline: `$` matched before \n
])
def test_hostile_slugs_are_refused(slug):
    assert valid_slug(slug) is False


def test_publish_records_a_pending_publication(store, tmp_path):
    src = tmp_path / "index.html"
    src.write_text("<html>rob</html>")

    pub = store.stage("rob", "rob-status", [str(src)])

    assert pub.status == "pending"
    assert pub.url == "https://pub.example.com/rob-status/"
    assert os.path.isfile(os.path.join(store.staging_dir_for("rob-status"), "index.html"))


def test_a_pending_publication_is_not_in_the_served_tree_at_all(store, tmp_path):
    """Defence in depth: the filesystem encodes the approval state, so the web
    server needs no application logic to keep an unapproved page unreachable."""
    src = tmp_path / "index.html"
    src.write_text("<html>rob</html>")
    store.stage("rob", "rob-status", [str(src)])

    assert store.is_live("rob-status") is False
    assert not os.path.exists(store.dir_for("rob-status"))


def test_the_staging_area_is_not_reachable_from_the_served_root(store):
    """The pending directory must not itself be servable as a slug."""
    from core.publish import valid_slug
    staging = os.path.basename(os.path.dirname(store.staging_dir_for("rob-status")))
    assert valid_slug(staging) is False


def test_approval_moves_it_into_the_served_tree(store, tmp_path):
    src = tmp_path / "index.html"
    src.write_text("<html>rob</html>")
    store.stage("rob", "rob-status", [str(src)])

    assert store.approve("rob", "rob-status") is True
    assert store.is_live("rob-status") is True
    assert os.path.isfile(os.path.join(store.dir_for("rob-status"), "index.html"))
    assert not os.path.exists(store.staging_dir_for("rob-status"))


def test_a_second_publish_of_an_approved_slug_needs_no_new_approval(store, tmp_path):
    """The owner approves a SLUG once; the agent then iterates on it freely."""
    src = tmp_path / "index.html"
    src.write_text("v1")
    store.stage("rob", "rob-status", [str(src)])
    store.approve("rob", "rob-status")

    src.write_text("v2 with more content")
    pub = store.stage("rob", "rob-status", [str(src)])

    assert pub.status == "live"
    assert store.is_live("rob-status") is True
    with open(os.path.join(store.dir_for("rob-status"), "index.html")) as f:
        assert f.read() == "v2 with more content"


def test_a_slug_belongs_to_one_tenant(store, tmp_path):
    src = tmp_path / "index.html"
    src.write_text("mine")
    store.stage("rob", "rob-status", [str(src)])

    with pytest.raises(PermissionError):
        store.stage("someone_else", "rob-status", [str(src)])


def test_another_tenant_cannot_approve(store, tmp_path):
    src = tmp_path / "index.html"
    src.write_text("mine")
    store.stage("rob", "rob-status", [str(src)])

    assert store.approve("someone_else", "rob-status") is False
    assert store.is_live("rob-status") is False
    assert not os.path.exists(store.dir_for("rob-status"))


def test_unpublish_takes_it_down(store, tmp_path):
    src = tmp_path / "index.html"
    src.write_text("bye")
    store.stage("rob", "rob-status", [str(src)])
    store.approve("rob", "rob-status")

    assert store.unpublish("rob", "rob-status") is True
    assert store.is_live("rob-status") is False
    assert not os.path.exists(store.dir_for("rob-status"))


def test_a_hostile_slug_never_reaches_the_filesystem(store, tmp_path):
    src = tmp_path / "index.html"
    src.write_text("x")

    with pytest.raises(ValueError):
        store.stage("rob", "../../etc/nginx", [str(src)])


def test_staging_refuses_a_file_outside_the_allowed_root(store, tmp_path):
    """A publish must never be able to copy /etc/polyrob/polyrob.env out."""
    with pytest.raises(ValueError):
        store.stage("rob", "leak", ["/etc/passwd"], confine_to=str(tmp_path))


# ---------------------------------------------------------------------------
# Coding-agent review B8 (2026-09-24): a static build output is a directory,
# and two same-name files must never overwrite each other silently.
# ---------------------------------------------------------------------------

def _build(ws):
    dist = ws / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<h1>hi</h1>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / ".DS_Store").write_text("junk")
    (dist / ".cache").mkdir()
    (dist / ".cache" / "x").write_text("junk")
    return dist


def test_a_directory_publishes_its_tree(store, tmp_path):
    ws = tmp_path / "ws"
    dist = _build(ws)
    pub = store.stage("u1", "site", [str(dist)], confine_to=str(ws))
    staged = store.staging_dir_for("site")
    assert (open(os.path.join(staged, "index.html")).read()) == "<h1>hi</h1>"
    assert os.path.isfile(os.path.join(staged, "assets", "app.js"))
    assert not os.path.exists(os.path.join(staged, ".DS_Store"))
    assert not os.path.exists(os.path.join(staged, ".cache"))
    assert pub.file_count == 2


def test_two_sources_with_one_name_are_refused(store, tmp_path):
    ws = tmp_path / "ws"
    (ws / "a").mkdir(parents=True)
    (ws / "b").mkdir()
    (ws / "a" / "index.html").write_text("A")
    (ws / "b" / "index.html").write_text("B")
    with pytest.raises(ValueError, match="two sources publish as"):
        store.stage("u1", "clash", [str(ws / "a" / "index.html"), str(ws / "b" / "index.html")],
                    confine_to=str(ws))


def test_a_directory_outside_the_workspace_is_refused(store, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = _build(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="outside"):
        store.stage("u1", "leak", [str(outside)], confine_to=str(ws))


def test_a_credential_inside_a_directory_refuses_the_publish(store, tmp_path):
    ws = tmp_path / "ws"
    dist = _build(ws)
    (dist / "server.pem").write_text("-----BEGIN PRIVATE KEY-----")
    with pytest.raises(ValueError, match="credential"):
        store.stage("u1", "cred", [str(dist)], confine_to=str(ws))


def test_a_symlink_escaping_the_workspace_inside_a_directory_is_refused(store, tmp_path):
    ws = tmp_path / "ws"
    dist = _build(ws)
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    (dist / "link.txt").symlink_to(secret)
    with pytest.raises(ValueError, match="outside"):
        store.stage("u1", "sym", [str(dist)], confine_to=str(ws))


# --- codex review 2026-09-25: vet-then-copy races ------------------------------

def test_a_file_swapped_after_vetting_is_refused(store, tmp_path, monkeypatch):
    import core.publish as publish_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    page = ws / "index.html"
    page.write_text("ok")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    real_copy = publish_mod._copy_vetted

    def swap_then_copy(src, dest, ident, budget):
        os.unlink(src)
        os.symlink(secret, src)  # the leaf becomes an outside symlink
        return real_copy(src, dest, ident, budget)
    monkeypatch.setattr(publish_mod, "_copy_vetted", swap_then_copy)
    with pytest.raises((ValueError, OSError)):
        store.stage("u1", "race", [str(page)], confine_to=str(ws))


def test_growth_after_vetting_cannot_beat_the_cap(store, tmp_path, monkeypatch):
    import core.publish as publish_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    page = ws / "index.html"
    page.write_text("small")
    monkeypatch.setattr(publish_mod, "MAX_PUBLISH_BYTES", 1000)
    real_copy = publish_mod._copy_vetted

    def grow_then_copy(src, dest, ident, budget):
        with open(src, "a") as f:
            f.write("x" * 5000)  # same inode, now past the cap
        return real_copy(src, dest, ident, budget)
    monkeypatch.setattr(publish_mod, "_copy_vetted", grow_then_copy)
    with pytest.raises(ValueError, match="too large"):
        store.stage("u1", "grow", [str(page)], confine_to=str(ws))


def test_a_failed_update_leaves_the_live_version_served(store, tmp_path, monkeypatch):
    # Codex review 2026-09-25: an update deleted the served tree BEFORE copying.
    import core.publish as publish_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "index.html").write_text("v1")
    store.stage("u1", "live", [str(ws / "index.html")], confine_to=str(ws))
    assert store.approve("u1", "live")
    served = os.path.join(store.dir_for("live"), "index.html")
    assert open(served).read() == "v1"

    (ws / "index.html").write_text("v2")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(publish_mod, "_copy_vetted", boom)
    with pytest.raises(OSError):
        store.stage("u1", "live", [str(ws / "index.html")], confine_to=str(ws))
    assert open(served).read() == "v1"
    assert not [d for d in os.listdir(store.root) if d.startswith(".build-")]

    monkeypatch.undo()
    store.stage("u1", "live", [str(ws / "index.html")], confine_to=str(ws))
    assert open(served).read() == "v2"
    assert not [d for d in os.listdir(store.root) if d.startswith((".build-", ".old-"))]
