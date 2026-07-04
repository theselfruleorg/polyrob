"""Webview avatar routes — serve the instance face LIVE (engine JS + /pfp.json +
/pfp.png), all fail-open (404 when no avatar). Isolated router client, per the
existing webgate-pages test pattern."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, home):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_pfp_data_dir", lambda: str(home))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app, raise_server_exceptions=True)


def _write_avatar(home):
    from core.instance import pfp_dir
    d = pfp_dir(home, "rob")
    d.mkdir(parents=True)
    (d / "pfp.png").write_bytes(b"\x89PNG\r\n\x1a\npng")
    (d / "pfp.json").write_text(json.dumps(
        {"generator": "mindprint@v2", "seed": "Rob Ottmachin", "variant": "",
         "traits": {"eyes": "square"}, "voice": {"pitch": 1.0, "rate": 1.0, "timbre": 0.5}}))


def test_pfp_json_404_when_no_avatar(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    assert c.get("/pfp.json").status_code == 404


def test_pfp_json_served_when_present(monkeypatch, tmp_path):
    _write_avatar(tmp_path)
    c = _client(monkeypatch, tmp_path)
    r = c.get("/pfp.json")
    assert r.status_code == 200
    assert r.json()["voice"]["pitch"] == 1.0


def test_pfp_png_404_then_200(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    assert c.get("/pfp.png").status_code == 404
    _write_avatar(tmp_path)
    r = c.get("/pfp.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_engine_js_served_same_origin(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.get("/avatar/mindprint.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "class Mindprint" in r.text
    assert "window.Mindprint" in r.text          # dual global exposure for the classic load


def test_live_embed_js_served(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.get("/avatar/avatar-live.js")
    assert r.status_code == 200
    assert "/pfp.json" in r.text


def test_pfp_data_dir_matches_the_cli_writer(monkeypatch, tmp_path):
    """generate<->serve must agree: env wins; else prefer the CLI's cwd/.polyrob default."""
    import webview.pages as pages

    monkeypatch.setenv("POLYROB_DATA_DIR", "/explicit/home")
    assert pages._pfp_data_dir() == "/explicit/home"

    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".polyrob").mkdir()
    assert pages._pfp_data_dir() == str(tmp_path / ".polyrob")  # where `pfp generate` writes

    # no env, no local .polyrob -> falls back to the generic webview data home
    monkeypatch.chdir(tmp_path / ".polyrob")   # a dir with no nested .polyrob
    assert pages._pfp_data_dir() == pages._data_dir()
