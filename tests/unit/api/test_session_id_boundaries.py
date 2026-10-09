from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from agents.task.path import PathManager


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['orchestrator_alpha123', 'a!', 'a' * 51, 'ab'])
async def test_all_external_resolvers_refuse_noncanonical_ids(tmp_path, monkeypatch, value):
    from api import dependencies, task_http_api
    from webview.session_access import http_session_id
    manager = PathManager(data_root=str(tmp_path))
    monkeypatch.setattr('agents.task.path.pm', lambda: manager)
    monkeypatch.setattr(dependencies, 'pm', lambda: manager)
    agent = SimpleNamespace(get_orchestrator=Mock())
    for call in [lambda: task_http_api.clean_session_id_at_entry(value),
                 lambda: http_session_id(value, manager)]:
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await dependencies.resolve_orchestrator(value, agent)
    assert exc.value.status_code == 400
    agent.get_orchestrator.assert_not_called()
