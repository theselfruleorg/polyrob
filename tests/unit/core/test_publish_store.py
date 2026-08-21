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
