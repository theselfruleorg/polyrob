"""``read_file`` loads the whole file to page it, and ``max_file_size`` had no reader: a
500 MB workspace file could take the agent process, and every session in it, past its
memory limit. The read stops at the cap, and the agent is told how to read a part."""
import pytest

from core.exceptions import ServiceError
from tools.filesystem import MAX_FILE_SIZE, FileSystem


def _write(path, size):
    with open(path, "wb") as f:
        f.truncate(size)
    return str(path)


def test_read_over_the_cap_refuses_and_loads_only_the_cap(tmp_path, monkeypatch):
    path = _write(tmp_path / "big.log", MAX_FILE_SIZE + 2)
    seen = {}
    import core.security.workspace_io as io_mod
    real = io_mod.read_bytes

    def spy(p, root, **kw):
        seen.update(kw)
        return real(p, root, **kw)
    monkeypatch.setattr(io_mod, "read_bytes", spy)
    with pytest.raises(ServiceError, match="read_file did not load it"):
        FileSystem._safe_read_text(path, str(tmp_path), max_bytes=MAX_FILE_SIZE)
    assert seen["max_bytes"] == MAX_FILE_SIZE + 1


def test_read_at_the_cap_works(tmp_path):
    path = _write(tmp_path / "ok.log", 1024)
    assert len(FileSystem._safe_read_text(path, str(tmp_path), max_bytes=1024)) == 1024


def test_other_callers_keep_the_whole_read(tmp_path):
    path = _write(tmp_path / "ok.log", 4096)
    assert len(FileSystem._safe_read_text(path, str(tmp_path))) == 4096
