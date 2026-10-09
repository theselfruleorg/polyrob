"""WAL-1: a child of the agent (same UID) may not use the signer socket.

The agent cannot drop the signer-clients group from its host children
(setgroups needs CAP_SETGID even to shrink), so the signer tells the agent's
MAIN process from its children: the earliest-started process of the UID in
its cgroup. These tests fake /proc and /sys/fs/cgroup.
"""
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.signer import protocol
from core.signer.caps import SignerConfigError, parse_signer_config, render_signer_toml
from core.signer.peer import is_main_process
from core.signer.server import SignerServer

AGENT = 996
CG = "/system.slice/polyrob.service"


def _proc(tmp_path, pid, uid, start, cg=CG):
    d = tmp_path / "proc" / str(pid)
    d.mkdir(parents=True)
    (d / "cgroup").write_text(f"0::{cg}\n")
    (d / "status").write_text(f"Name:\tpython\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 10
    (d / "stat").write_text(f"{pid} (py (x) y) " + " ".join(fields) + "\n")


def _cgroup(tmp_path, pids, cg=CG):
    d = tmp_path / "cg" / cg.lstrip("/")
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))


def _check(tmp_path, pid, uid=AGENT):
    return is_main_process(pid, uid, proc_root=str(tmp_path / "proc"),
                           cgroup_root=str(tmp_path / "cg"))


def test_main_process_passes_and_children_are_refused(tmp_path):
    _proc(tmp_path, 100, AGENT, 5000)      # polyrob telegram (main)
    _proc(tmp_path, 200, AGENT, 9000)      # an MCP stdio child / shell command
    _proc(tmp_path, 300, AGENT, 9500)      # a double-forked grandchild (PPid 1)
    _proc(tmp_path, 50, 0, 100)            # an ExecStartPre=+ root helper, earlier
    _cgroup(tmp_path, [50, 100, 200, 300])
    assert _check(tmp_path, 100) is True
    assert _check(tmp_path, 200) is False
    assert _check(tmp_path, 300) is False


def test_owner_cli_in_its_session_scope_still_passes(tmp_path):
    scope = "/user.slice/user-0.slice/session-7.scope"
    _proc(tmp_path, 10, 0, 100, cg=scope)       # sudo (root)
    _proc(tmp_path, 11, AGENT, 200, cg=scope)   # sudo -u polyrob-agent polyrob …
    _cgroup(tmp_path, [10, 11], cg=scope)
    assert _check(tmp_path, 11) is True


def test_cannot_tell_answers_none(tmp_path):
    assert _check(tmp_path, 4321) is None                 # hidden / vanished pid
    _proc(tmp_path, 100, AGENT, 5000, cg="")
    (tmp_path / "proc" / "100" / "cgroup").write_text("1:name=systemd:/x\n")   # v1 only
    assert _check(tmp_path, 100) is None
    assert is_main_process(0, AGENT) is None


def _server(mode):
    return SignerServer(SimpleNamespace(config=SimpleNamespace(peer_check=mode)), "/unused")


@pytest.mark.parametrize("mode,verdict,allowed", [
    ("main_process", False, False), ("main_process", True, True),
    ("main_process", None, True),   # cannot tell -> UID fallback, warned
    ("warn", False, True), ("uid", False, True),
])
def test_server_peer_process_modes(monkeypatch, mode, verdict, allowed):
    monkeypatch.setattr("core.signer.server.peer_pid_of", lambda conn: 4242)
    monkeypatch.setattr("core.signer.peer.is_main_process", lambda pid, uid: verdict)
    assert _server(mode).peer_process_allowed(object(), AGENT) is allowed


def test_root_is_never_checked(monkeypatch):
    monkeypatch.setattr("core.signer.peer.is_main_process", lambda pid, uid: False)
    monkeypatch.setattr("core.signer.server.peer_pid_of", lambda conn: 4242)
    assert _server("main_process").peer_process_allowed(object(), 0) is True


def test_refused_child_gets_peer_refused_before_the_request_is_read(monkeypatch):
    sent = []
    service = SimpleNamespace(config=SimpleNamespace(peer_check="main_process"),
                              peer_allowed=lambda uid: True, handle=Mock())
    server = SignerServer(service, "/unused")
    monkeypatch.setattr("core.signer.server.peer_uid_of", lambda conn: AGENT)
    monkeypatch.setattr("core.signer.server.peer_pid_of", lambda conn: 4242)
    monkeypatch.setattr("core.signer.peer.is_main_process", lambda pid, uid: False)
    monkeypatch.setattr("core.signer.server.protocol.send_frame", lambda c, f: sent.append(f))
    recv = Mock()
    monkeypatch.setattr("core.signer.server.protocol.recv_frame", recv)
    conn = Mock()
    conn.__enter__ = lambda s: s
    conn.__exit__ = lambda s, *a: None
    server._serve_conn(conn)
    assert sent and sent[0]["code"] == protocol.PEER_REFUSED
    recv.assert_not_called()
    service.handle.assert_not_called()


def _cfg(**server):
    return {"caps": {"per_tx_usd": 10.0, "daily_usd": 20.0},
            "server": {"client_uids": [AGENT], **server}}


def test_signer_toml_peer_check_defaults_to_warn_and_round_trips():
    import tomllib
    assert parse_signer_config(_cfg()).peer_check == "warn"
    cfg = parse_signer_config(_cfg(peer_check="main_process"))
    assert parse_signer_config(tomllib.loads(render_signer_toml(cfg))).peer_check == "main_process"
    with pytest.raises(SignerConfigError):
        parse_signer_config(_cfg(peer_check="pid"))


def test_client_socket_is_not_inherited_by_children(monkeypatch, tmp_path):
    """A host child must never inherit an open signer connection (PEP 446)."""
    from core.signer.client import SignerClient
    seen = []
    real = socket.socket

    class Spy(real):
        def connect(self, path):
            seen.append(self.get_inheritable())
            raise OSError("no signer here")

    monkeypatch.setattr("core.signer.client.socket.socket", Spy)
    with pytest.raises(Exception):
        SignerClient(str(tmp_path / "s.sock")).call("ping")
    assert seen == [False]
