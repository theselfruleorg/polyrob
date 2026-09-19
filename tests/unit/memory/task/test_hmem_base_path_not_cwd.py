"""H-MEM's base path is never CWD-relative when DATA_PATH is unset.

Prod (2026-09-17, post identity cutover): ``TaskContextManager`` defaulted to
``Path("data") / "auto"`` — relative to the process CWD, ``/opt/polyrob``, which
``ProtectSystem=strict`` makes read-only. Every hierarchical-memory save raised
``[Errno 30] Read-only file system: 'data/auto/rob/sessions/<sid>'`` (155 in 24 h);
Rob's per-session H-MEM was silently lost on every resume. And ``DATA_PATH`` is
not a declared ``BotConfig`` field, so on a real deploy it is ALWAYS unset.
Unset → the data home (the N1 rule: never CWD-relative); an explicit
DATA_PATH still wins.
"""
from pathlib import Path

from modules.memory.task.task_context_manager import TaskContextManager


class _Cfg:
    def __init__(self, values):
        self.data = dict(values)

    def get(self, key, default=None):
        return self.data.get(key, default)


def test_unset_data_path_anchors_under_the_data_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    monkeypatch.chdir(cwd)
    mgr = TaskContextManager(name="t", config=_Cfg({"HMEM_SEMANTIC": "off"}))
    assert mgr._base_path.is_absolute()
    assert mgr._base_path == Path(home) / "auto"
    assert not str(mgr._base_path).startswith(str(cwd))


def test_explicit_data_path_still_wins(tmp_path):
    mgr = TaskContextManager(name="t", config=_Cfg({"DATA_PATH": str(tmp_path / "x"),
                                                    "HMEM_SEMANTIC": "off"}))
    assert mgr._base_path == tmp_path / "x" / "auto"
