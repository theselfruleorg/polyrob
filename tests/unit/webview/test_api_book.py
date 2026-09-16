"""GET /api/webgate/book — one reader over every money chain (043 A34).

The route is a thin, tenant-scoped delegator to ``tools.defi.book.read_book``
(the loop over chains lives there — tools tier, since core may not import
``tools.*``). This file pins the route contract: it resolves the effective
user id (403 in multitenant with no identity), calls ``read_book`` with that
id + the data dir, and returns its result verbatim. The worst-chain-wins
verdict logic itself is covered by
``tests/unit/tools/defi/test_book_reader.py`` against ``read_book`` directly.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, user_id="u1"):
    import webview.pages as pages
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", user_id)
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


def test_api_book_passes_effective_user_and_returns_result(monkeypatch, tmp_path):
    client, pages = _client(monkeypatch, tmp_path, user_id="u1")

    calls = {}

    async def fake_read_book(user_id, data_dir, **kwargs):
        calls["user_id"] = user_id
        calls["data_dir"] = data_dir
        return {"chains": {"base": {"verdict": "clean", "report": {},
                                     "portfolio_text": "x", "error": None}},
                "verdict": "clean", "checked_at": 1234.5,
                "ledger_path": str(tmp_path / "ledger.md")}

    monkeypatch.setattr(pages, "read_book", fake_read_book)

    res = client.get("/api/webgate/book")
    assert res.status_code == 200
    body = res.json()
    assert body["verdict"] == "clean"
    assert body["chains"]["base"]["verdict"] == "clean"
    assert calls["user_id"] == "u1"
    assert calls["data_dir"] == str(tmp_path)


def test_api_book_disabled_flag_shape(monkeypatch, tmp_path):
    client, pages = _client(monkeypatch, tmp_path)

    async def fake_read_book(user_id, data_dir, **kwargs):
        return {"chains": {}, "verdict": None, "checked_at": 1.0,
                "ledger_path": None,
                "error": ("DEFI_DATA_ENABLED is off — the book cannot be "
                          "read until an operator enables on-chain sight")}

    monkeypatch.setattr(pages, "read_book", fake_read_book)

    res = client.get("/api/webgate/book")
    assert res.status_code == 200
    body = res.json()
    assert body["chains"] == {}
    assert body["verdict"] is None
    assert "DEFI_DATA_ENABLED is off" in body["error"]
