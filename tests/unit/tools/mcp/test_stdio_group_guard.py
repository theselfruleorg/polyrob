"""A reaped leader's pid can belong to a stranger: its group gets no signal."""
import os
from types import SimpleNamespace

from tools.mcp import stdio_group


def test_a_reused_pid_of_a_reaped_leader_gets_no_signal(monkeypatch):
    sent = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(stdio_group, "_pid_in_use", lambda pid: True)
    assert stdio_group._signal_group(SimpleNamespace(pid=4321, returncode=0), 15) is False
    assert sent == []


def test_grandchildren_of_a_reaped_leader_still_get_the_signal(monkeypatch):
    sent = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(stdio_group, "_pid_in_use", lambda pid: False)
    assert stdio_group._signal_group(SimpleNamespace(pid=4321, returncode=0), 15) is True
    assert sent == [(4321, 15)]
