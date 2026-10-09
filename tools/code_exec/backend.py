"""Execution backend seam (Item 3 — WS-C1, trimmed).

An ``ExecutionBackend`` runs an ``ExecutionRequest`` and returns an
``ExecutionResult``. ``ExecutionBackendRegistry`` resolves a backend by name
(mirrors ``modules/memory/provider.py::MemoryProviderRegistry``) — register a
factory by name, ``create`` by the ``CODE_EXEC_BACKEND`` key.

Future-backend contract (C5, deferred): a hard sandbox (Docker/Modal/gVisor)
implements the same ABC — ``setup`` provisions the sandbox, ``run`` executes inside
it, ``teardown`` reaps it, ``capabilities`` advertises ``{"network": bool,
"isolation": "process"|"container"|...}``. Only ``local_subprocess`` ships now.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional

from tools.code_exec.result import ExecutionRequest, ExecutionResult


class ExecutionBackendError(RuntimeError):
    """Raised for unknown-backend / registry misuse."""


class ExecutionBackend(ABC):
    """Runs code and returns captured output. One implementation ships (local)."""

    #: backend registry key
    name: str = "base"

    @abstractmethod
    async def setup(self) -> None:
        """Provision any resources (no-op for local subprocess)."""

    @abstractmethod
    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute ``request`` and return the captured result."""

    @abstractmethod
    async def teardown(self) -> None:
        """Release resources."""

    @property
    def capabilities(self) -> Dict[str, object]:
        """Advertise backend traits (network access, isolation level, ...)."""
        return {"network": True, "isolation": "process"}


class ExecutionBackendRegistry:
    """Name -> backend-factory registry; ``create`` resolves a fresh instance."""

    def __init__(self) -> None:
        self._factories: Dict[str, Callable[[], ExecutionBackend]] = {}
        #: 073 W6: called once with an unknown name before ``create`` gives up —
        #: the default registry pulls pack backends here
        #: (``tools/code_exec/pack_backends.py::discover``).
        self.on_miss: Optional[Callable[[str], None]] = None

    def register(self, name: str, factory: Callable[[], ExecutionBackend]) -> None:
        self._factories[name] = factory

    def unregister(self, name: str) -> None:
        self._factories.pop(name, None)

    def create(self, name: str) -> ExecutionBackend:
        factory = self._factories.get(name)
        if factory is None and self.on_miss is not None:
            try:
                self.on_miss(name)
            except Exception:
                pass
            factory = self._factories.get(name)
        if factory is None:
            raise ExecutionBackendError(
                f"unknown execution backend '{name}' (known: {sorted(self._factories)})"
            )
        return factory()

    @property
    def names(self) -> List[str]:
        return sorted(self._factories)


# Process-wide default registry. The local backend self-registers on import of
# tools.code_exec (see __init__).
default_registry = ExecutionBackendRegistry()
