"""Provisioned servers never turn a local .complete marker into trusted code."""
def test_provisioned_custody_server_does_not_activate_writable_local_overlay(tmp_path, monkeypatch):
    import core.lazy_deps as ld
    system = tmp_path/'system'
    local = tmp_path/'local'
    (system/'requests').mkdir(parents=True)
    monkeypatch.setattr(ld, 'system_root', lambda: system)
    monkeypatch.setattr(ld, 'local_root', lambda: local)
    monkeypatch.setattr(ld, '_custody', lambda: True)
    monkeypatch.setattr(ld, '_local_mode', lambda: False)
    feature = local/ld._lock_digest()/'provider.anthropic'
    (feature/'lib').mkdir(parents=True)
    (feature/'.complete').touch()
    monkeypatch.setattr('sys.path', list(__import__('sys').path))
    assert str(feature/'lib') not in ld.activate_overlay()



def _virtual_trusted_overlay(monkeypatch):
    import os
    import pwd
    import stat
    from pathlib import Path
    from types import SimpleNamespace
    import core.lazy_deps as ld
    feature = Path('/var/lib/polyrob/pylibs/0123456789abcdef/provider.anthropic')
    library = feature / 'lib'
    package = library / 'example.py'
    complete = feature / '.complete'
    metadata = {p: SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
                for p in feature.parents}
    for p in [feature, library]:
        metadata[p] = SimpleNamespace(st_uid=990, st_mode=stat.S_IFDIR | 0o755)
    for p in [package, complete]:
        metadata[p] = SimpleNamespace(st_uid=990, st_mode=stat.S_IFREG | 0o644)
    metadata[Path('/var/lib/polyrob')].st_mode = stat.S_IFDIR | 0o3771
    monkeypatch.setattr(pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=990))
    monkeypatch.setattr(Path, 'lstat', lambda p: metadata[p])
    monkeypatch.setattr(os, 'walk', lambda *a, **k: iter([
        (str(feature), ['lib'], ['.complete']), (str(library), [], ['example.py'])]))
    # Root has write access everywhere; this must not define agent authority.
    monkeypatch.setattr(os, 'access', lambda *a, **k: True)
    return ld, feature, package, metadata


def test_protected_overlay_accepts_trusted_tree_even_for_root(monkeypatch):
    ld, feature, _, _ = _virtual_trusted_overlay(monkeypatch)
    assert ld._protected_overlay(feature)


def test_protected_overlay_rejects_writable_module(monkeypatch):
    import stat
    ld, feature, package, metadata = _virtual_trusted_overlay(monkeypatch)
    metadata[package].st_mode |= stat.S_IWGRP
    assert not ld._protected_overlay(feature)


def test_protected_overlay_rejects_replaceable_parent(monkeypatch):
    from pathlib import Path
    import stat
    ld, feature, _, metadata = _virtual_trusted_overlay(monkeypatch)
    metadata[Path('/var/lib/polyrob')].st_mode &= ~stat.S_ISVTX
    assert not ld._protected_overlay(feature)


def test_protected_overlay_rejects_symlink_and_untrusted_owner(monkeypatch):
    import stat
    ld, feature, package, metadata = _virtual_trusted_overlay(monkeypatch)
    metadata[package].st_mode = stat.S_IFLNK | 0o777
    assert not ld._protected_overlay(feature)
    metadata[package].st_mode = stat.S_IFREG | 0o644
    metadata[package].st_uid = 1234
    assert not ld._protected_overlay(feature)


def test_trusted_system_overlay_activates_under_privileged_checks(tmp_path, monkeypatch):
    import os
    import pwd
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    import core.lazy_deps as ld
    root = tmp_path / 'pylibs'
    (root / 'requests').mkdir(parents=True)
    feature = root / ld._lock_digest() / 'provider.anthropic'
    (feature / 'lib').mkdir(parents=True)
    (feature / '.complete').touch()
    original = Path.lstat
    def ownership(path):
        info = original(path)
        return SimpleNamespace(st_mode=info.st_mode,
                               st_uid=990 if path == root or root in path.parents else 0)
    monkeypatch.setattr(Path, 'lstat', ownership)
    monkeypatch.setattr(pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=990))
    monkeypatch.setattr(os, 'access', lambda *a, **k: True)
    monkeypatch.setattr(ld, 'system_root', lambda: root)
    monkeypatch.setattr(ld, 'local_root', lambda: tmp_path / 'local')
    monkeypatch.setattr(sys, 'path', list(sys.path))
    assert str(feature / 'lib') in ld.activate_overlay()
