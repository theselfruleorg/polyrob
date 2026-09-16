import sys

import pytest

from tools import knowledge_inventory as inventory


def test_fallback_bounds_all_entries_including_directories(tmp_path, monkeypatch):
    for idx in range(25):
        (tmp_path / str(idx)).mkdir()
    monkeypatch.setattr(inventory, 'MAX_ENTRIES', 5)
    paths, capped = inventory.walk_files(tmp_path, recursive=True)
    assert paths == []
    assert capped


def test_fallback_bounds_depth_and_closes_iterators(tmp_path, monkeypatch):
    nested = tmp_path / 'a' / 'b' / 'c'
    nested.mkdir(parents=True)
    (nested / 'deep.md').write_text('deep')
    monkeypatch.setattr(inventory, 'MAX_DEPTH', 2)
    paths, capped = inventory.walk_files(tmp_path, recursive=True)
    assert not paths
    assert capped


def test_fallback_does_not_follow_directory_symlinks(tmp_path):
    (tmp_path / 'cycle').symlink_to(tmp_path, target_is_directory=True)
    paths, capped = inventory.walk_files(tmp_path, recursive=True)
    assert paths == [tmp_path / 'cycle']
    assert not capped


def test_rg_preserves_newline_filename(tmp_path):
    source = tmp_path / 'line\nbreak.md'
    source.write_text('note')
    result = inventory.rg_files(tmp_path)
    if result is None:
        pytest.skip('rg unavailable')
    assert result == ([source], False)


@pytest.mark.parametrize('mode', ['flood', 'hang'])
def test_rg_timeout_or_output_cap_stops_child(tmp_path, monkeypatch, mode):
    original = inventory.subprocess.Popen
    children = []
    def spawn(*args, **kwargs):
        code = 'import os; os.write(1, b"x" * 100000); import time; time.sleep(30)' if mode == 'flood' else 'import time; time.sleep(30)'
        proc = original([sys.executable, '-c', code], **kwargs)
        children.append(proc)
        return proc
    monkeypatch.setattr(inventory.subprocess, 'Popen', spawn)
    monkeypatch.setattr(inventory, 'MAX_OUTPUT_BYTES', 1024)
    monkeypatch.setattr(inventory, 'TIMEOUT_SECONDS', 0.2)
    assert inventory.rg_files(tmp_path) == ([], True)
    assert children[0].poll() is not None


def test_binary_candidates_charge_the_read_budget(tmp_path, monkeypatch):
    from tools.knowledge_ingest import _iter_files
    (tmp_path / 'a.bin').write_bytes(b'\0' * 16)
    (tmp_path / 'b.bin').write_bytes(b'\0' * 16)
    files, skipped = _iter_files(tmp_path, recursive=False, max_bytes=16)
    assert not files
    assert skipped['binary'] == 1
    assert skipped['max_bytes'] == 1
