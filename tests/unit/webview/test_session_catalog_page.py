import json
from webview.session_catalog import session_page


def test_page_hydrates_only_requested_rows_and_scopes(tmp_path, monkeypatch):
    for user in ('alice', 'bob'):
        for n in range(12):
            p = tmp_path / user / str(n)
            (p/'feed').mkdir(parents=True)
            (p/'task.json').write_text(json.dumps({'task':f'{user} task {n}'}))
            (p/'status.json').write_text('{"status":"running"}')
    from pathlib import Path
    reads = []
    original = Path.read_text
    def spy(path, *args, **kw):
        reads.append(path)
        return original(path, *args, **kw)
    monkeypatch.setattr(Path, 'read_text', spy)
    body = session_page(tmp_path, 'user', 'alice', limit=3)
    assert len(body['sessions']) == 3
    assert body['next_offset'] == 3
    assert len(reads) == 6
    assert all(row['user'] == 'alice' for row in body['sessions'])
    second = session_page(tmp_path, 'user', 'alice', offset=3, limit=3)
    assert not {r['id'] for r in body['sessions']} & {r['id'] for r in second['sessions']}


def test_unreadable_status_never_becomes_completed(tmp_path):
    (tmp_path/'alice'/'s'/'feed').mkdir(parents=True)
    body = session_page(tmp_path, 'user', 'alice')
    assert body['sessions'][0]['status'] is None
    assert 'status' in body['sessions'][0]['unreadable']
    assert session_page(tmp_path/'missing', 'all', 'alice')['total'] is None
    assert session_page(tmp_path, 'none', '')['sessions'] == []


def test_route_scopes_tenant_and_remains_readable_in_readonly(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from webview import pages_new, server
    monkeypatch.setenv('POLYROB_POSTURE', 'multitenant')
    monkeypatch.setenv('WEBVIEW_READ_ONLY', 'true')
    monkeypatch.setattr(server, 'pm', lambda: SimpleNamespace(data_root=tmp_path))
    monkeypatch.setattr(server, '_annotate_runtime', lambda rows: rows)
    for user in ('alice', 'bob'):
        (tmp_path/user/'session'/'feed').mkdir(parents=True)
    app = FastAPI()
    @app.middleware('http')
    async def identity(request, call_next):
        request.state.authenticated = True
        request.state.user_id = 'alice'
        return await call_next(request)
    app.include_router(pages_new.api_router)
    response = TestClient(app).get('/api/webgate/chats?offset=-1&limit=1000')
    assert response.status_code == 200
    assert [row['user'] for row in response.json()['sessions']] == ['alice']


def test_route_refuses_missing_multitenant_identity(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from webview.pages_new import api_router
    monkeypatch.setenv('POLYROB_POSTURE', 'multitenant')
    app = FastAPI()
    app.include_router(api_router)
    assert TestClient(app).get('/api/webgate/chats').status_code == 403
