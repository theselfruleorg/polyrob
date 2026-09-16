import os
from pathlib import Path

import pytest

from core.security.confined_read import read_confined_bytes


def test_regular_file_and_exact_size(tmp_path):
    path = tmp_path / 'note.txt'
    path.write_bytes(b'hello')
    assert read_confined_bytes(path, tmp_path, 5) == b'hello'
    with pytest.raises(OSError):
        read_confined_bytes(path, tmp_path, 4)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'parent_link', 'fifo', 'outside'])
def test_unsafe_files_refused_without_reading(tmp_path, kind):
    root = tmp_path / 'workspace'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    hidden = outside / 'note.txt'
    hidden.write_bytes(b'private fixture')
    candidate = root / 'note.txt'
    if kind == 'symlink':
        candidate.symlink_to(hidden)
    elif kind == 'hardlink':
        os.link(hidden, candidate)
    elif kind == 'parent_link':
        (root / 'nested').symlink_to(outside, target_is_directory=True)
        candidate = root / 'nested' / 'note.txt'
    elif kind == 'fifo':
        os.mkfifo(candidate)
    else:
        candidate = hidden
    with pytest.raises(OSError):
        read_confined_bytes(candidate, root, 100)


def test_parent_replaced_after_open_cannot_redirect_read(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    root.mkdir()
    parent = root / 'nested'
    parent.mkdir()
    (parent / 'note.txt').write_bytes(b'original')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'note.txt').write_bytes(b'private fixture')
    original_open = os.open
    def swapping_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == 'nested':
            parent.rename(root / 'saved')
            parent.symlink_to(outside, target_is_directory=True)
        return fd
    monkeypatch.setattr(os, 'open', swapping_open)
    monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {swapping_open})
    assert read_confined_bytes(parent / 'note.txt', root, 100) == b'original'


def test_binary_sniff_reads_only_prefix(tmp_path, monkeypatch):
    from core.security.secret_guard import is_binary_file
    path = tmp_path / 'large.unknown'
    path.write_bytes(b'a' * 5000 + b'\0')
    monkeypatch.setattr(Path, 'read_bytes', lambda *a: pytest.fail('unbounded read'))
    assert not is_binary_file(path)
    assert is_binary_file(path, sample=b'\0')
