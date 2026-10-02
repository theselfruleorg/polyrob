"""070 W0.13 — a cold open with no language model says so instead of a silent 200.

The console process on prod (and at local posture) has no ``llm`` service. Before
this, ``POST /api/task/sessions`` answered 200, the page navigated to an empty
chat, and nothing ever answered. Now an auto-start with no model is refused with
503 ``no_model`` before any session folder exists.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import task_http_api
from api.task_http_api import _no_model, create_session


class _Container:
    def __init__(self, llm):
        self._llm = llm

    def get_service(self, name):
        return self._llm if name == "llm" else None


class _Exploding:
    def get_service(self, name):
        raise RuntimeError("probe blew up")


def _agent(container):
    return SimpleNamespace(container=container)


def _run(body, agent):
    return asyncio.run(create_session(body, SimpleNamespace(state=SimpleNamespace()), agent=agent))


def test_no_llm_service_is_503_no_model(monkeypatch):
    def _never(*a, **k):
        raise AssertionError("payment must not run: the refusal comes first")
    import api.payment_verification as pv
    monkeypatch.setattr(pv, "verify_payment_for_request", _never)
    with pytest.raises(HTTPException) as caught:
        _run({"task": "are u here?"}, _agent(_Container(None)))
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "no_model"


def test_llm_present_starts_as_before(monkeypatch):
    """With a model, the handler goes on to the payment check as before."""
    import api.payment_verification as pv

    async def _teapot(*a, **k):
        raise HTTPException(status_code=418, detail="reached payment")
    monkeypatch.setattr(pv, "verify_payment_for_request", _teapot)
    with pytest.raises(HTTPException) as caught:
        _run({"task": "hi"}, _agent(_Container(object())))
    assert caught.value.status_code == 418


def test_probe_failure_does_not_block():
    assert _no_model(_agent(_Exploding())) is False
    assert _no_model(SimpleNamespace()) is False
    assert _no_model(None) is False
    assert _no_model(_agent(_Container(None))) is True


def test_wait_for_uploads_is_not_refused(monkeypatch):
    import api.payment_verification as pv

    async def _teapot(*a, **k):
        raise HTTPException(status_code=418, detail="reached payment")
    monkeypatch.setattr(pv, "verify_payment_for_request", _teapot)
    for body in ({"task": "hi", "wait_for_uploads": True}, {"task": "hi", "auto_start": False}):
        with pytest.raises(HTTPException) as caught:
            _run(body, _agent(_Container(None)))
        assert caught.value.status_code == 418, body


def test_the_module_exposes_the_probe():
    assert task_http_api._no_model is _no_model
