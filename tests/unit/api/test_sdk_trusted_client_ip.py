"""L-16 (audit 2026-11-21): the pack SDK's ``get_client_ip`` is the TRUSTED client
address — a forged ``X-Forwarded-For`` from an untrusted peer never reaches a
pack's audit row — and it stays a plain FastAPI dependency (``request`` only, so a
POST route's body is never re-shaped by an extra parameter)."""
from fastapi import Depends, FastAPI
from fastapi.dependencies.utils import get_dependant
from fastapi.testclient import TestClient
from pydantic import BaseModel

from polyrob import sdk


def test_sdk_client_ip_ignores_a_forged_forwarded_header():
    app = FastAPI()

    class Body(BaseModel):
        x: int = 0

    @app.post("/p")
    def route(body: Body, ip: str = Depends(sdk.get_client_ip)):
        return {"ip": ip, "x": body.x}

    r = TestClient(app).post("/p", json={"x": 3}, headers={"X-Forwarded-For": "9.9.9.9"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ip": "testclient", "x": 3}


def test_sdk_client_ip_takes_only_the_request():
    d = get_dependant(path="/p", call=sdk.get_client_ip)
    assert not d.query_params and not d.body_params and not d.header_params


def test_sdk_client_ip_is_a_string_without_a_client():
    class _Req:
        client = None
        headers = {}
    assert sdk.get_client_ip(_Req()) == "unknown"
