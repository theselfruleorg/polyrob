from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from api.api_docs import install_docs
from api.openai_compat.router import router


def app():
    instance = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_docs(instance)
    instance.include_router(router)
    return instance


@pytest.mark.parametrize('path', ['/docs', '/redoc', '/openapi.json', '/v1/models'])
def test_anonymous_metadata_requests_require_authentication(path):
    with TestClient(app()) as client:
        assert client.get(path).status_code == 401


@pytest.mark.parametrize('path', ['/docs', '/redoc', '/openapi.json', '/v1/models'])
def test_authenticated_metadata_remains_available(path):
    instance = app()

    @instance.middleware('http')
    async def authenticated(request: Request, call_next):
        request.state.user_id = 'alice'
        return await call_next(request)

    with TestClient(instance) as client:
        assert client.get(path).status_code == 200
