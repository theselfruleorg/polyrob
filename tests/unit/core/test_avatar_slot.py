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
    st = avatar.load_avatar(tmp_path, "alice")
    assert st.is_set and st.is_default and st.is_raster
    assert st.path == avatar.DEFAULT_AVATAR and st.path.is_file()
    assert st.source == avatar.DEFAULT_SOURCE
    assert avatar.avatar_image_path(tmp_path, "alice") == avatar.DEFAULT_AVATAR
    assert avatar.describe(st).startswith("the default")
    # nothing is written into the home just by reading
    assert not avatar.avatar_dir(tmp_path, "alice").exists()


def test_with_no_shipped_default_an_empty_home_reads_as_not_set(tmp_path, monkeypatch):
    monkeypatch.setattr(avatar, "DEFAULT_AVATAR", tmp_path / "missing.png")
    st = avatar.load_avatar(tmp_path, "alice")
    assert st.state == "none" and not st.is_set
    assert avatar.avatar_image_path(tmp_path, "alice") is None
    assert avatar.describe(st) == "not set"


def test_set_writes_the_image_and_its_record(tmp_path):
    st = avatar.set_avatar(tmp_path, "alice", PNG, source="file:face.png")
    assert st.is_set and st.is_raster
    assert st.path == avatar.avatar_dir(tmp_path, "alice") / "avatar.png"
    assert st.content_type == "image/png" and st.source == "file:face.png"
    assert len(st.sha256) == 64
    assert avatar.describe(st) == "set (file:face.png)"


def test_a_new_image_replaces_the_old_one(tmp_path):
    avatar.set_avatar(tmp_path, "alice", PNG, source="a")
    st = avatar.set_avatar(tmp_path, "alice", JPG, source="b")
    names = sorted(p.name for p in avatar.avatar_dir(tmp_path, "alice").iterdir())
    assert names == ["avatar.jpg", "avatar.json"]
    assert st.path.name == "avatar.jpg"


def test_svg_is_accepted_but_is_not_raster(tmp_path):
    st = avatar.set_avatar(tmp_path, "alice", SVG, source="nft:base:0x1:1")
    assert st.is_set and not st.is_raster


@pytest.mark.parametrize("data", [b"", b"hello world", b"%PDF-1.7"])
def test_a_non_image_is_refused(tmp_path, data):
    with pytest.raises(avatar.AvatarError):
        avatar.set_avatar(tmp_path, "alice", data, source="x")
    assert avatar.load_avatar(tmp_path, "alice").is_default


def test_an_oversized_image_is_refused(tmp_path):
    with pytest.raises(avatar.AvatarError):
        avatar.set_avatar(tmp_path, "alice", PNG + b"\x00" * avatar.MAX_BYTES, source="x")


def test_a_broken_record_reads_as_unreadable_not_absent(tmp_path):
    avatar.set_avatar(tmp_path, "alice", PNG, source="x")
    (avatar.avatar_dir(tmp_path, "alice") / "avatar.json").write_text("{not json")
    st = avatar.load_avatar(tmp_path, "alice")
    assert st.state == "unreadable"
    assert avatar.describe(st).startswith("unreadable")


def test_a_record_whose_image_is_gone_is_unreadable(tmp_path):
    st = avatar.set_avatar(tmp_path, "alice", PNG, source="x")
    st.path.unlink()
    assert avatar.load_avatar(tmp_path, "alice").state == "unreadable"


def test_clear_returns_to_the_default(tmp_path):
    avatar.set_avatar(tmp_path, "alice", PNG, source="x")
    assert not avatar.load_avatar(tmp_path, "alice").is_default
    assert avatar.clear_avatar(tmp_path, "alice") is True
    assert avatar.load_avatar(tmp_path, "alice").is_default
    assert avatar.clear_avatar(tmp_path, "alice") is False


def test_an_unsafe_instance_id_does_not_traverse(tmp_path):
    d = avatar.avatar_dir(tmp_path, "../../etc")
    assert tmp_path in d.parents and ".." not in d.parts


@pytest.mark.asyncio
async def test_a_file_reference_needs_a_named_origin(tmp_path):
    """120 W1: ONE reference branch. A file is read at the terminal, or from the
    agent's workspace — never from a bare chat line."""
    f = tmp_path / "me.png"
    f.write_bytes(PNG)
    data, source = await avatar_sources.image_from_ref(str(f), local_files=True)
    assert (data, source) == (PNG, "file:me.png")
    with pytest.raises(avatar.AvatarError, match="no such file"):
        await avatar_sources.image_from_ref(str(tmp_path / "missing.png"), local_files=True)
    with pytest.raises(avatar.AvatarError, match="cannot be read from here"):
        await avatar_sources.image_from_ref(str(f))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "in.png").write_bytes(PNG)
    assert (await avatar_sources.image_from_ref("in.png", workspace=str(ws)))[1] == "file:in.png"
    with pytest.raises(avatar.AvatarError):
        await avatar_sources.image_from_ref(str(f), workspace=str(ws))     # outside it


@pytest.mark.asyncio
async def test_the_reference_branch_routes_urls_and_nfts(monkeypatch):
    seen = []

    async def _nft(chain, contract, token_id):
        seen.append(("nft", chain, token_id))
        return PNG, "nft"

    async def _url(url):
        seen.append(("url", url))
        return PNG, "url"

    monkeypatch.setattr(avatar_sources, "image_from_nft", _nft)
    monkeypatch.setattr(avatar_sources, "image_from_url", _url)
    await avatar_sources.image_from_ref("nft:base:0x" + "ab" * 20 + ":7")
    await avatar_sources.image_from_ref("ipfs://bafy/x.png")
    await avatar_sources.image_from_ref("data:image/png;base64,AAAA")
    assert seen == [("nft", "base", 7), ("url", "ipfs://bafy/x.png"),
                    ("url", "data:image/png;base64,AAAA")]
    with pytest.raises(avatar.AvatarError):
        await avatar_sources.image_from_ref("  ")


@pytest.mark.asyncio
async def test_my_own_identity_token_cannot_be_the_source(monkeypatch):
    monkeypatch.setenv("A2A_BASE_URL", "https://agent.example")
    meta = json.dumps({"image": "https://agent.example/avatar.png"}).encode()

    async def _fetch(url, **_kw):
        return meta

    monkeypatch.setattr(avatar_sources, "_token_uri", lambda c, a, t: "https://agent.example/eip8004/registration.json")
    monkeypatch.setattr(avatar_sources, "fetch_bytes", _fetch)
    with pytest.raises(avatar.AvatarError, match="own avatar slot"):
        await avatar_sources.image_from_nft("base", "0x" + "ab" * 20, 1)


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


# --- 120 W0: the errors name the step, the host and the node -----------------

class _Res:
    def __init__(self, status, body=b"", final_url=""):
        self.status, self.body, self.final_url = status, body, final_url


@pytest.mark.asyncio
async def test_a_refused_fetch_names_the_step_and_the_host(monkeypatch):
    import core.security.safe_fetch as fetcher

    async def _fetch(url, **_kw):
        return _Res(403, final_url=url)

    monkeypatch.setattr(fetcher, "safe_fetch", _fetch)
    with pytest.raises(avatar.AvatarError) as e:
        await avatar_sources.fetch_bytes("https://meta.example/t/1.json",
                                         what="the token metadata")
    assert str(e.value) == "meta.example answered HTTP 403 for the token metadata"


def test_a_node_that_cannot_be_read_is_named_not_reported_as_no_metadata(monkeypatch):
    import core.wallet.onchain as onchain

    def _rpc(url, method, args, timeout):
        raise RuntimeError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(onchain, "_rpc", _rpc)
    monkeypatch.setattr(onchain, "rpc_url_for_chain", lambda chain: "https://rpc.example/v1/key")
    with pytest.raises(avatar.AvatarError) as e:
        avatar_sources._token_uri("base", "0x" + "ab" * 20, 1)
    text = str(e.value)
    assert "node at rpc.example could not be read" in text and "403" in text
    assert "key" not in text                       # the host only, never the URL path


def test_a_token_with_no_uri_on_a_node_that_answered_says_so(monkeypatch):
    import core.wallet.onchain as onchain
    monkeypatch.setattr(onchain, "_rpc", lambda url, method, args, timeout: "0x")
    monkeypatch.setattr(onchain, "rpc_url_for_chain", lambda chain: "https://rpc.example")
    with pytest.raises(avatar.AvatarError) as e:
        avatar_sources._token_uri("base", "0x" + "ab" * 20, 1)
    assert "has no metadata URI" in str(e.value)


def test_the_ipfs_gateway_has_one_reader(monkeypatch):
    monkeypatch.setenv("IPFS_GATEWAY", "https://gw.example/ipfs")
    assert avatar_sources._gateway("ipfs://bafy/1.png") == "https://gw.example/ipfs/bafy/1.png"
    assert avatar_sources._gateway("ipfs://ipfs/bafy") == "https://gw.example/ipfs/bafy"
    monkeypatch.delenv("IPFS_GATEWAY")
    assert avatar_sources._gateway("ipfs://bafy") == "https://ipfs.io/ipfs/bafy"
    from modules.eip8004.registration import get_eip8004_config
    assert get_eip8004_config().ipfs_gateway == "https://ipfs.io/ipfs/"


# --- 120 T2: one answer for every origin that serves /avatar.png -------------

def test_image_response_parts(tmp_path, monkeypatch):
    avatar.set_avatar(tmp_path, "alice", JPG, source="file:f.jpg")
    path, media_type, headers = avatar.image_response_parts(tmp_path, "alice")
    assert path.name == "avatar.jpg" and media_type == "image/jpeg"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" not in headers
    avatar.set_avatar(tmp_path, "alice", SVG, source="file:f.svg")
    _p, media_type, headers = avatar.image_response_parts(tmp_path, "alice")
    assert media_type == "image/svg+xml" and "sandbox" in headers["Content-Security-Policy"]
    monkeypatch.setattr(avatar, "DEFAULT_AVATAR", tmp_path / "missing.png")
    assert avatar.image_response_parts(tmp_path, "nobody") is None


def test_the_api_serves_the_avatar_without_a_credential(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.auth_constants import is_public_path
    from api.avatar_endpoint import router
    import core.instance as instance
    import core.runtime_paths as rp

    monkeypatch.setattr(rp, "resolve_data_home", lambda *a, **k: tmp_path)
    monkeypatch.setattr(instance, "resolve_instance_id", lambda *a, **k: "alice")
    avatar.set_avatar(tmp_path, "alice", PNG, source="file:face.png")
    app = FastAPI()
    app.include_router(router)
    res = TestClient(app).get("/avatar.png")
    assert res.status_code == 200 and res.headers["content-type"] == "image/png"
    assert res.content == PNG
    assert TestClient(app).head("/avatar.png").status_code == 200
    assert is_public_path("/avatar.png") and not is_public_path("/avatar.png/x")
