"""098 AG-05: compaction checkpoints never overwrite one another."""
import json
import logging
import os
from types import SimpleNamespace

import pytest

from agents.task.agent.messages import compactor
from agents.task.agent.messages.compactor import CompactorMixin


class _MM(CompactorMixin):
    def __init__(self, d):
        self._compaction_checkpoint_dir = str(d)
        self._compaction_count = 0
        self.logger = logging.getLogger("t")


def _msgs(text):
    return [SimpleNamespace(content=text)]


def test_index_seeds_from_existing_files(tmp_path):
    for n in range(3):
        (tmp_path / f"compaction_{n}.json").write_text("[]")
    mm = _MM(tmp_path)          # a fresh manager after a restart / recreate
    mm._write_compaction_checkpoint(_msgs("new"))
    assert json.loads((tmp_path / "compaction_3.json").read_text())[0]["content"] == "new"
    assert (tmp_path / "compaction_0.json").read_text() == "[]"


def test_two_managers_same_session_never_overwrite(tmp_path):
    a, b = _MM(tmp_path), _MM(tmp_path)
    a._write_compaction_checkpoint(_msgs("parent"))
    b._write_compaction_checkpoint(_msgs("child"))
    contents = sorted(json.loads(p.read_text())[0]["content"] for p in tmp_path.glob("compaction_*.json"))
    assert contents == ["child", "parent"]


def test_checkpoint_write_is_atomic(tmp_path, monkeypatch):
    def _boom(path, obj, **k):
        raise OSError("disk full")
    monkeypatch.setattr(compactor, "atomic_write_json", _boom)
    _MM(tmp_path)._write_compaction_checkpoint(_msgs("x"))   # fail-open
    assert list(tmp_path.iterdir()) == []


def test_child_checkpoint_dir_is_a_subdir(tmp_path, monkeypatch):
    import agents.task.path as path_mod
    monkeypatch.setattr(path_mod, "pm", lambda: SimpleNamespace(get_history_dir=lambda sid: tmp_path))
    d = compactor.child_checkpoint_dir("sess", "sub_agent/1")
    assert d == os.path.join(str(tmp_path), "sub_agent_1")
