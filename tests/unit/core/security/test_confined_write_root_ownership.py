"""A ROOT-run confined write must leave the file readable by the service group.

Prod 2026-09-18 12:25Z: `polyrob owner promote skill …` run from a root shell
promoted three skills as root:root 0700/0600 under the tenant store, so the
agent (polyrob-agent, ProtectSystem=strict) could not load what the owner had
just approved. The 09-16 identity split made every root-side admin verb a
potential trap of this shape; the deployer already answers it with
"data group + 2770/660". The writer applies the same rule: when euid is 0 and
the parent directory belongs to a non-root group, new files/dirs take that
group and become group-accessible. A non-root caller is untouched.
"""
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.security import confined_write as cw


def _fake_stat(gid):
    st = MagicMock()
    st.st_gid = gid
    st.st_uid = 0
    return st


def test_root_write_matches_parent_group_and_opens_group_bits(tmp_path, monkeypatch):
    monkeypatch.setattr(cw.os, "geteuid", lambda: 0)
    real_fstat = os.fstat
    monkeypatch.setattr(cw.os, "fstat", lambda fd: _fake_stat(4242))
    chown = MagicMock(); chmod = MagicMock()
    monkeypatch.setattr(cw.os, "chown", chown); monkeypatch.setattr(cw.os, "chmod", chmod)
    target = tmp_path / "user_rob" / "skill-x" / "SKILL.md"
    cw.write_confined_text(target, tmp_path, "hello")
    assert target.read_text() == "hello"
    # the new directories and the file were re-grouped to the parent's gid …
    groups = {c.args[2] for c in chown.call_args_list}
    assert groups == {4242}
    # … and made group-accessible (dirs 0770, file 0660)
    modes = sorted({c.args[1] for c in chmod.call_args_list})
    assert modes == [0o660, 0o770]


def test_root_write_under_a_root_group_parent_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(cw.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cw.os, "fstat", lambda fd: _fake_stat(0))
    chown = MagicMock(); monkeypatch.setattr(cw.os, "chown", chown)
    cw.write_confined_text(tmp_path / "a" / "b.txt", tmp_path, "x")
    chown.assert_not_called()


def test_non_root_write_never_chowns(tmp_path, monkeypatch):
    monkeypatch.setattr(cw.os, "geteuid", lambda: 1000)
    chown = MagicMock(); monkeypatch.setattr(cw.os, "chown", chown)
    cw.write_confined_text(tmp_path / "a" / "b.txt", tmp_path, "x")
    chown.assert_not_called()
    assert (tmp_path / "a" / "b.txt").read_text() == "x"
