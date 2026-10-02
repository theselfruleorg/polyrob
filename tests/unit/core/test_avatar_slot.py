"""The avatar primitive (core/avatar.py): one image slot, no generator."""
import base64
import json

import pytest

from core import avatar
from tools import avatar_sources

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'


def test_an_empty_home_shows_the_default_mark(tmp_path):
    st = avatar.load_avatar(tmp_path, "rob")
    assert st.is_set and st.is_default and st.is_raster
    assert st.path == avatar.DEFAULT_AVATAR and st.path.is_file()
    assert st.source == avatar.DEFAULT_SOURCE
    assert avatar.avatar_image_path(tmp_path, "rob") == avatar.DEFAULT_AVATAR
    assert avatar.describe(st).startswith("the default")
    # nothing is written into the home just by reading
    assert not avatar.avatar_dir(tmp_path, "rob").exists()


def test_with_no_shipped_default_an_empty_home_reads_as_not_set(tmp_path, monkeypatch):
    monkeypatch.setattr(avatar, "DEFAULT_AVATAR", tmp_path / "missing.png")
    st = avatar.load_avatar(tmp_path, "rob")
    assert st.state == "none" and not st.is_set
    assert avatar.avatar_image_path(tmp_path, "rob") is None
    assert avatar.describe(st) == "not set"


def test_set_writes_the_image_and_its_record(tmp_path):
    st = avatar.set_avatar(tmp_path, "rob", PNG, source="file:face.png")
    assert st.is_set and st.is_raster
    assert st.path == avatar.avatar_dir(tmp_path, "rob") / "avatar.png"
    assert st.content_type == "image/png" and st.source == "file:face.png"
    assert len(st.sha256) == 64
    assert avatar.describe(st) == "set (file:face.png)"


def test_a_new_image_replaces_the_old_one(tmp_path):
    avatar.set_avatar(tmp_path, "rob", PNG, source="a")
    st = avatar.set_avatar(tmp_path, "rob", JPG, source="b")
    names = sorted(p.name for p in avatar.avatar_dir(tmp_path, "rob").iterdir())
    assert names == ["avatar.jpg", "avatar.json"]
    assert st.path.name == "avatar.jpg"


def test_svg_is_accepted_but_is_not_raster(tmp_path):
    st = avatar.set_avatar(tmp_path, "rob", SVG, source="nft:base:0x1:1")
    assert st.is_set and not st.is_raster


@pytest.mark.parametrize("data", [b"", b"hello world", b"%PDF-1.7"])
def test_a_non_image_is_refused(tmp_path, data):
    with pytest.raises(avatar.AvatarError):
        avatar.set_avatar(tmp_path, "rob", data, source="x")
    assert avatar.load_avatar(tmp_path, "rob").is_default


def test_an_oversized_image_is_refused(tmp_path):
    with pytest.raises(avatar.AvatarError):
        avatar.set_avatar(tmp_path, "rob", PNG + b"\x00" * avatar.MAX_BYTES, source="x")


def test_a_broken_record_reads_as_unreadable_not_absent(tmp_path):
    avatar.set_avatar(tmp_path, "rob", PNG, source="x")
    (avatar.avatar_dir(tmp_path, "rob") / "avatar.json").write_text("{not json")
    st = avatar.load_avatar(tmp_path, "rob")
    assert st.state == "unreadable"
    assert avatar.describe(st).startswith("unreadable")


def test_a_record_whose_image_is_gone_is_unreadable(tmp_path):
    st = avatar.set_avatar(tmp_path, "rob", PNG, source="x")
    st.path.unlink()
    assert avatar.load_avatar(tmp_path, "rob").state == "unreadable"


def test_clear_returns_to_the_default(tmp_path):
    avatar.set_avatar(tmp_path, "rob", PNG, source="x")
    assert not avatar.load_avatar(tmp_path, "rob").is_default
    assert avatar.clear_avatar(tmp_path, "rob") is True
    assert avatar.load_avatar(tmp_path, "rob").is_default
    assert avatar.clear_avatar(tmp_path, "rob") is False


def test_an_unsafe_instance_id_does_not_traverse(tmp_path):
    d = avatar.avatar_dir(tmp_path, "../../etc")
    assert tmp_path in d.parents and ".." not in d.parts


def test_set_from_file(tmp_path):
    f = tmp_path / "me.png"
    f.write_bytes(PNG)
    st = avatar.set_avatar_from_file(tmp_path / "home", "rob", f)
    assert st.is_set and st.source == "file:me.png"
    with pytest.raises(avatar.AvatarError):
        avatar.set_avatar_from_file(tmp_path / "home", "rob", tmp_path / "missing.png")


@pytest.mark.asyncio
async def test_a_data_uri_is_decoded_locally():
    uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
    data, source = await avatar_sources.image_from_url(uri)
    assert data == PNG and source.startswith("url:data:")


@pytest.mark.asyncio
async def test_a_non_web_scheme_is_refused():
    with pytest.raises(avatar.AvatarError):
        await avatar_sources.fetch_bytes("file:///etc/passwd")


@pytest.mark.asyncio
async def test_an_nft_with_on_chain_metadata_resolves_to_its_image(monkeypatch):
    meta = {"name": "polyrob #1",
            "image": "data:image/svg+xml;base64," + base64.b64encode(SVG).decode()}
    uri = "data:application/json;base64," + base64.b64encode(json.dumps(meta).encode()).decode()
    monkeypatch.setattr(avatar_sources, "_token_uri", lambda c, a, t: uri)
    data, source = await avatar_sources.image_from_nft("base", "0x" + "ab" * 20, 1)
    assert data == SVG
    assert source == f"nft:base:0x{'ab' * 20}:1"


def test_parse_nft_ref():
    assert avatar_sources.parse_nft_ref("base:0x" + "ab" * 20 + ":42") == ("base", "0x" + "ab" * 20, 42)
    for bad in ("base:0xabc:1", "base:0x" + "ab" * 20, "base:0x" + "ab" * 20 + ":x"):
        with pytest.raises(avatar.AvatarError):
            avatar_sources.parse_nft_ref(bad)
