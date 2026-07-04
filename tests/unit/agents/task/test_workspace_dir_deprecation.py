"""B-T6 — collapse the workspace_dir dual.

The sync ``workspace_dir`` property is canonical; the async ``get_workspace_dir``
shim now emits a DeprecationWarning while still returning the same value, so callers
migrate to the property without a behaviour break.
"""
import pytest

from agents.task.session.workspace import WorkspaceMixin


class _WS(WorkspaceMixin):
    def __init__(self):
        self._workspace_dir = "/tmp/ws"


@pytest.mark.asyncio
async def test_async_shim_warns_but_returns_property_value():
    ws = _WS()
    with pytest.warns(DeprecationWarning):
        result = await ws.get_workspace_dir()
    assert result == "/tmp/ws" == ws.workspace_dir
