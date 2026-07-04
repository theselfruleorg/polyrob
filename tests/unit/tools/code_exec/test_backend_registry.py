"""Item 3 — execution backend registry."""
import pytest

from tools.code_exec.backend import ExecutionBackendRegistry, ExecutionBackendError
from tools.code_exec import default_registry, resolve_backend
from tools.code_exec.backends.local_subprocess import LocalSubprocessBackend


def test_resolve_known_backend_by_name():
    reg = ExecutionBackendRegistry()
    reg.register("local_subprocess", LocalSubprocessBackend)
    backend = reg.create("local_subprocess")
    assert isinstance(backend, LocalSubprocessBackend)


def test_unknown_backend_raises_clear_error():
    reg = ExecutionBackendRegistry()
    with pytest.raises(ExecutionBackendError):
        reg.create("does_not_exist")


def test_default_registry_has_local_backend():
    assert "local_subprocess" in default_registry.names
    assert isinstance(resolve_backend(), LocalSubprocessBackend)
