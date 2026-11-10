"""113 W6 item 6 (G-2): offers and orders on Money.

The read is the owner's offers (with what each sold) and the newest orders, the
rows ``/offer`` and ``/offer orders`` print; an unreadable store is named, never
"no offers". The write is only pause and resume, run as the ``/offer`` line
through the owner-verb plane (``console_commands.run_console_line``).
"""
import copy

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.commerce import store as st

MANIFEST = {
    "id": "custom-art", "title": "Custom art NFT", "summary": "A 1/1 art piece minted to you.",
    "pricing": {"kind": "fixed", "amount_usd": "5"},
    "rails": [{"asset": "usdc-base", "mode": "transfer"}],
    "inputs": {"delivery_address": {"type": "string", "format": "evm_address",
                                    "role": "recipient"}},
    "deliverable": {"kind": "onchain_asset", "proof": "tx_receipt"},
    "fulfillment": {"playbook": "offer-custom-art", "tools": ["defi_trade"],
                    "budget_per_order": {"spend_usd": 0.5}, "deadline": "PT30M"},
    "listings": ["a2a"],
}


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    import webview.pages as pages
    import webview.commerce_routes as mod
    import webview.console_commands as cc
    import webview.server as server
    path = str(tmp_path / "commerce.db")
    monkeypatch.setattr(st, "db_path", lambda data_dir=None: path)
    lines, audits = [], []

    async def fake_line(task_agent, clean_id, user_id, text):
        lines.append((clean_id, user_id, text))
        return "✅ Offer custom-art is PAUSED." if "pause" in text else "❌ No offer x."

    monkeypatch.setattr(cc, "run_console_line", fake_line)
    monkeypatch.setattr(server, "_in_process_task_agent", lambda: object())
    monkeypatch.setattr(pages, "_wallet_owner_id", lambda req: "owner1")
    monkeypatch.setattr(mod, "console_write", lambda kind, **kw: audits.append((kind, kw)))
    app = FastAPI()
    app.include_router(mod.router)
    return type("R", (), {"client": TestClient(app), "lines": lines, "audits": audits,
                          "path": path, "pages": pages})


def test_no_store_yet_is_an_empty_list_that_was_read(rig):
    body = rig.client.get("/api/webgate/offers").json()
    assert body == {"readable": True, "offers": [], "orders": [], "orders_truncated": False,
                    "user_id": "owner1"}


def test_the_owner_sees_their_offers_and_no_other_owners(rig):
    st.save_draft("owner1", MANIFEST, path=rig.path)
    other = copy.deepcopy(MANIFEST)
    other["id"] = "not-mine"
    st.save_draft("someone-else", other, path=rig.path)
    body = rig.client.get("/api/webgate/offers").json()
    assert [o["id"] for o in body["offers"]] == ["custom-art"]
    assert body["offers"][0] == {**body["offers"][0], "state": "draft", "title": "Custom art NFT",
                                 "sold": 0, "version": 0}


def test_an_unreadable_store_is_not_no_offers(rig, monkeypatch):
    def boom(*a, **k):
        raise st.CommerceUnreadable("commerce store unreadable: disk I/O error")
    monkeypatch.setattr(st, "list_offers", boom)
    body = rig.client.get("/api/webgate/offers").json()
    assert body["readable"] is False and "disk I/O error" in body["error"]
    assert "offers" not in body


def test_pause_runs_the_offer_line_on_the_owner_plane(rig):
    r = rig.client.post("/api/webgate/offers/custom-art/pause")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert rig.lines == [("money", "owner1", "/offer pause custom-art")]
    assert rig.audits[0][0] == "console_offer_verb"
    assert rig.audits[0][1]["attrs"] == {"offer_id": "custom-art", "verb": "pause", "ok": True}


def test_a_refusal_is_shown_verbatim(rig):
    r = rig.client.post("/api/webgate/offers/x/resume")
    assert r.status_code == 409 and r.json()["message"] == "❌ No offer x."


@pytest.mark.parametrize("verb", ["retire", "live", "setup", "resolve", "quote"])
def test_only_pause_and_resume_are_here(rig, verb):
    assert rig.client.post(f"/api/webgate/offers/custom-art/{verb}").status_code == 404
    assert rig.lines == []


def test_a_bad_offer_id_never_reaches_the_plane(rig):
    assert rig.client.post("/api/webgate/offers/a%20b/pause").status_code == 404
    assert rig.lines == []


def test_a_caller_who_is_not_the_wallet_owner_is_refused(rig, monkeypatch):
    def refuse(req):
        raise HTTPException(status_code=403, detail="not the owner")
    monkeypatch.setattr(rig.pages, "_wallet_owner_id", refuse)
    assert rig.client.get("/api/webgate/offers").status_code == 403
    assert rig.client.post("/api/webgate/offers/custom-art/pause").status_code == 403
    assert rig.lines == []


def test_the_write_carries_the_console_guards():
    import webview.commerce_routes as mod
    from webview import webgate
    post = [r for r in mod.router.routes if "POST" in r.methods][0]
    deps = {d.dependency.__name__ for d in post.dependencies}
    assert {g.dependency.__name__ for g in webgate.MUTATION_DEPS} <= deps
