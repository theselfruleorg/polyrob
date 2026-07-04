import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.surfaces.inbound_webhook import WebhookSurface
from core.surfaces.idempotency import IdempotencyStore
from core.surfaces.envelopes import InboundMessage, Identity, SessionSource


class _Container:
    def __init__(self, svc): self._svc = svc
    def get_service(self, k): return self._svc.get(k)


class _WA(WebhookSurface):
    @property
    def surface_id(self): return "whatsapp"
    def verify_signature(self, headers, body): return True
    def verify_challenge(self, params):
        return params.get("hub.challenge") if params.get("hub.verify_token") == "vt" else None
    def parse(self, payload): return []
    def idempotency_key(self, inbound): return inbound.idempotency_key


def _app(tmp_path):
    from api.webhooks import router, set_container_provider
    wa = _WA(IdempotencyStore(str(tmp_path / "i.db")))
    container = _Container({"webhook_surfaces": {"whatsapp": wa},
                            "task_agent": object()})
    set_container_provider(lambda: container)
    app = FastAPI(); app.include_router(router)
    return TestClient(app)


def test_get_challenge_echoes_when_token_matches(tmp_path):
    c = _app(tmp_path)
    r = c.get("/webhooks/whatsapp", params={"hub.verify_token": "vt", "hub.challenge": "42"})
    assert r.status_code == 200 and r.text.strip('"') == "42"


def test_get_challenge_403_on_bad_token(tmp_path):
    c = _app(tmp_path)
    r = c.get("/webhooks/whatsapp", params={"hub.verify_token": "no", "hub.challenge": "42"})
    assert r.status_code == 403


def test_post_unknown_surface_404(tmp_path):
    c = _app(tmp_path)
    r = c.post("/webhooks/nope", content=b"{}")
    assert r.status_code == 404
