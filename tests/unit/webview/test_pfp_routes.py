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


# ---- web setup routes: the same one-time draft -> randomize -> keep contract ----

def _fast_render(monkeypatch):
    """Skip Chromium in tests: fake the exact-engine renderer (store falls through
    to it first); the pure meta/lock mechanics are what's under test."""
    from modules.pfp import store
    from pathlib import Path

    def fake(config, out_png, *, size=None):
        Path(out_png).parent.mkdir(parents=True, exist_ok=True)
        Path(out_png).write_bytes(b"\x89PNG\r\n\x1a\nweb")
    monkeypatch.setattr(store, "render_still", fake)


def test_web_setup_generate_randomize_keep_flow(monkeypatch, tmp_path):
    _fast_render(monkeypatch)
    c = _client(monkeypatch, tmp_path)

    r = c.post("/api/pfp/generate")                       # mint a random draft
    body = r.json()
    assert r.status_code == 200 and body["ok"], body
    assert body["meta"]["locked"] is False
    first_variant = body["meta"]["variant"]
    assert first_variant.startswith("#")

    r = c.post("/api/pfp/randomize", json={"what": "all"})  # re-roll while draft
    body = r.json()
    assert body["ok"], body
    assert body["meta"]["variant"] != first_variant

    r = c.post("/api/pfp/randomize", json={"what": "voice"})  # voice only
    body = r.json()
    assert body["ok"] and "voice" in body["meta"]["override"]
    kept_variant = body["meta"]["variant"]

    r = c.post("/api/pfp/keep")                           # accept forever
    body = r.json()
    assert body["ok"] and body["meta"]["locked"] is True

    r = c.post("/api/pfp/randomize", json={"what": "all"})  # setup is over
    body = r.json()
    assert body["ok"] is False and "once" in body["message"]
    from core.instance import load_pfp_meta
    assert load_pfp_meta(tmp_path, "rob")["variant"] == kept_variant   # unchanged


def test_web_setup_generate_is_idempotent(monkeypatch, tmp_path):
    _fast_render(monkeypatch)
    c = _client(monkeypatch, tmp_path)
    v1 = c.post("/api/pfp/generate").json()["meta"]["variant"]
    body = c.post("/api/pfp/generate").json()             # second call: no new roll
    assert body["ok"] and body["meta"]["variant"] == v1


def test_web_setup_refused_in_read_only_console(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    c = _client(monkeypatch, tmp_path)
    assert c.post("/api/pfp/generate").status_code == 403
    assert c.post("/api/pfp/randomize").status_code == 403
    assert c.post("/api/pfp/keep").status_code == 403


def test_web_keep_without_avatar_is_a_soft_error(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    body = c.post("/api/pfp/keep").json()
    assert body["ok"] is False and "generate" in body["message"]
