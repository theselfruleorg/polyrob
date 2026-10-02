"""Keep the dev-mode ``/install`` dirs (H02: now OUTSIDE the workspace, under the
data home) inside a per-test tmp dir so no test writes to a real data home."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_install_root(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("sandbox_installs")
    monkeypatch.setattr("tools.code_exec.backends.docker._install_root", lambda: str(root))
    return str(root)
