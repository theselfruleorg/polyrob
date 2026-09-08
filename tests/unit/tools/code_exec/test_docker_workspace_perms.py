"""The forced-unprivileged container uid must be able to WRITE the files the
host process created, not just enter its directories.

Prod 2026-09-07: `polyrob.service` runs as root, so `DockerBackend` forces the
sandbox container to uid 65534 and `_ensure_workspace_writable` widens the
workspace. It walked `dirs` only, so every file the host-side coding/filesystem
tool wrote stayed root-owned 0644 — 9,246 of them under the live project tree —
and an in-container `shell_run` editing one hard-failed EACCES (journal:
`PermissionError: '/workspace/rob-status/trackrecord.html'`, repeatedly, with the
agent burning steps on it).
"""
import os
import stat

from tools.code_exec.backends.docker import DockerBackend, widen_mode_for_container


class TestWidenMode:
    def test_regular_file_gains_group_and_other_write(self):
        assert widen_mode_for_container(0o644) == 0o666

    def test_executable_file_stays_executable_for_everyone(self):
        assert widen_mode_for_container(0o755) == 0o777

    def test_non_executable_file_never_gains_an_exec_bit(self):
        assert widen_mode_for_container(0o600) & 0o111 == 0

    def test_already_wide_mode_is_unchanged(self):
        assert widen_mode_for_container(0o666) == 0o666
        assert widen_mode_for_container(0o777) == 0o777

    def test_setuid_and_other_high_bits_are_not_propagated(self):
        # Only the low 9 permission bits are ours to widen.
        assert widen_mode_for_container(0o644) & ~0o777 == 0


class TestEnsureWorkspaceWritable:
    def _as_root_owned(self, monkeypatch, root):
        """Report uid 0 for everything under *root* (tests don't run as root).

        Both `stat` (directories) and `lstat` (files — the walk must never follow a
        symlink) are patched, because the production code deliberately uses each.
        """
        def _rooted(real):
            def fake(path, *a, **kw):
                st = real(path, *a, **kw)
                if str(path).startswith(str(root)):
                    return os.stat_result((st.st_mode, st.st_ino, st.st_dev,
                                           st.st_nlink, 0, 0, st.st_size,
                                           st.st_atime, st.st_mtime, st.st_ctime))
                return st
            return fake

        monkeypatch.setattr(os, "stat", _rooted(os.stat))
        monkeypatch.setattr(os, "lstat", _rooted(os.lstat))

    def test_host_written_files_become_container_writable(self, tmp_path, monkeypatch):
        nested = tmp_path / "rob-status"
        nested.mkdir()
        page = nested / "trackrecord.html"
        page.write_text("<html></html>")
        os.chmod(page, 0o644)
        top = tmp_path / "notes.md"
        top.write_text("x")
        os.chmod(top, 0o644)
        self._as_root_owned(monkeypatch, tmp_path)

        DockerBackend._ensure_workspace_writable(str(tmp_path))

        assert stat.S_IMODE(os.lstat(page).st_mode) & 0o022 == 0o022
        assert stat.S_IMODE(os.lstat(top).st_mode) & 0o022 == 0o022

    def test_an_executable_script_keeps_its_exec_bit(self, tmp_path, monkeypatch):
        script = tmp_path / "run.sh"
        script.write_text("#!/bin/sh\n")
        os.chmod(script, 0o755)
        self._as_root_owned(monkeypatch, tmp_path)

        DockerBackend._ensure_workspace_writable(str(tmp_path))

        assert stat.S_IMODE(os.lstat(script).st_mode) == 0o777

    def test_a_symlink_is_never_followed(self, tmp_path, monkeypatch):
        outside = tmp_path.parent / "outside_secret.txt"
        outside.write_text("secret")
        os.chmod(outside, 0o600)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "link").symlink_to(outside)
        self._as_root_owned(monkeypatch, ws)

        DockerBackend._ensure_workspace_writable(str(ws))

        assert stat.S_IMODE(os.lstat(outside).st_mode) == 0o600

    def test_a_file_owned_by_the_container_uid_is_left_alone(self, tmp_path):
        # Real (non-root) ownership in the test process stands in for the
        # container's own uid: it can already write its own files.
        f = tmp_path / "made_by_container.txt"
        f.write_text("x")
        os.chmod(f, 0o600)

        DockerBackend._ensure_workspace_writable(str(tmp_path))

        assert stat.S_IMODE(os.lstat(f).st_mode) == 0o600
