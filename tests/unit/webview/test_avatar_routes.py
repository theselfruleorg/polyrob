"""Webview avatar routes — /avatar.json + /avatar.png read the one image slot
(core/avatar.py). Read-only: the console shows the face, it never sets it."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.unit.webview.owner_session import owner_headers

from core import avatar
from core.instance import resolve_instance_id

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _client(monkeypatch, home):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_avatar_data_dir", lambda: str(home))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app, raise_server_exceptions=True)


def test_avatar_json_shows_the_default_when_not_set(monkeypatch, tmp_path):
    r = _client(monkeypatch, tmp_path).get("/avatar.json")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "set" and body["is_default"] is True
    assert body["source"] == avatar.DEFAULT_SOURCE


def test_avatar_json_says_none_without_the_shipped_default(monkeypatch, tmp_path):
    monkeypatch.setattr(avatar, "DEFAULT_AVATAR", tmp_path / "missing.png")
    c = _client(monkeypatch, tmp_path)
    assert c.get("/avatar.json").json()["state"] == "none"
    assert c.get("/avatar.png").status_code == 404


def test_avatar_json_reports_the_source_when_set(monkeypatch, tmp_path):
    avatar.set_avatar(tmp_path, resolve_instance_id(), PNG, source="file:me.png")
    body = _client(monkeypatch, tmp_path).get("/avatar.json").json()
    assert body["state"] == "set" and body["is_default"] is False
    assert body["source"] == "file:me.png"
    assert body["content_type"] == "image/png"
    assert len(body["sha256"]) == 64


def test_an_unreadable_record_is_reported_not_hidden(monkeypatch, tmp_path):
    avatar.set_avatar(tmp_path, resolve_instance_id(), PNG, source="x")
    (avatar.avatar_dir(tmp_path, resolve_instance_id()) / "avatar.json").write_text("{")
    c = _client(monkeypatch, tmp_path)
    body = c.get("/avatar.json").json()
    assert body["state"] == "unreadable" and body["detail"]
    assert c.get("/avatar.png").status_code == 404


def test_avatar_png_default_then_own(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.get("/avatar.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content == avatar.DEFAULT_AVATAR.read_bytes()
    avatar.set_avatar(tmp_path, resolve_instance_id(), PNG, source="x")
    r = c.get("/avatar.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content == PNG


def test_an_svg_avatar_is_served_with_a_script_free_csp(monkeypatch, tmp_path):
    avatar.set_avatar(tmp_path, resolve_instance_id(), SVG, source="nft:base:0x1:1")
    r = _client(monkeypatch, tmp_path).get("/avatar.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["content-security-policy"].startswith("default-src 'none'")


def test_the_svg_csp_survives_the_console_security_middleware(monkeypatch, tmp_path):
    """The global header middleware must not replace the route's script-free CSP
    with the console policy (which allows CDN scripts) — through the FULL app."""
    import webview.pages as pages
    import webview.server as server
    monkeypatch.setattr(pages, "_avatar_data_dir", lambda: str(tmp_path))
    avatar.set_avatar(tmp_path, resolve_instance_id(), SVG, source="nft:base:0x1:1")
    r = TestClient(server._fastapi, headers=owner_headers(monkeypatch)).get("/avatar.png")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert csp.startswith("default-src 'none'") and "sandbox" in csp
    assert "script-src" not in csp

def test_the_generator_routes_are_gone(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    for path in ("/pfp.json", "/pfp.png", "/avatar/mindprint.js", "/avatar/avatar-live.js"):
        assert c.get(path).status_code == 404, path
    for path in ("/api/pfp/generate", "/api/pfp/randomize", "/api/pfp/keep"):
        assert c.post(path).status_code in (404, 405), path


def test_avatar_data_dir_matches_the_cli_writer(monkeypatch, tmp_path):
    """set<->serve must agree: env wins; else prefer the CLI's cwd/.polyrob default."""
    import webview.pages as pages

    monkeypatch.setenv("POLYROB_DATA_DIR", "/explicit/home")
    assert pages._avatar_data_dir() == "/explicit/home"

    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".polyrob").mkdir()
    assert pages._avatar_data_dir() == str(tmp_path / ".polyrob")

    monkeypatch.chdir(tmp_path / ".polyrob")   # a dir with no nested .polyrob
    assert pages._avatar_data_dir() == pages._data_dir()


def test_avatar_png_answers_head(monkeypatch, tmp_path):
    """A HEAD probe (a link checker, a proxy) gets the GET headers, not a 405."""
    avatar.set_avatar(tmp_path, resolve_instance_id(), PNG, source="x")
    c = _client(monkeypatch, tmp_path)
    r = c.head("/avatar.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == b""
    assert c.get("/avatar.png").content == PNG
