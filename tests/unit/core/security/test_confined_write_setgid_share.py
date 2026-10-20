"""A NON-root write (the service user, polyrob-agent) into a setgid shared
store left the file 0600: _share_with_parent_group only acted for root.
Prod 2026-10-15 05:07: `owner promote skill promo-bounty` as polyrob-agent
left SKILL.md 600 while its 28 siblings are 660.
"""
import os
import stat

import core.security.confined_write as cw


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_non_root_write_in_setgid_dir_is_group_shared(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    os.chmod(store, 0o2770)
    monkeypatch.setattr(cw.os, "geteuid", lambda: 1000)
    cw.write_confined_text(store / "SKILL.md", tmp_path, "x")
    assert _mode(store / "SKILL.md") == 0o660


def test_non_root_write_in_plain_dir_stays_private(tmp_path, monkeypatch):
    store = tmp_path / "plain"
    store.mkdir()
    os.chmod(store, 0o700)
    monkeypatch.setattr(cw.os, "geteuid", lambda: 1000)
    cw.write_confined_text(store / "f.txt", tmp_path, "x")
    assert _mode(store / "f.txt") == 0o600
