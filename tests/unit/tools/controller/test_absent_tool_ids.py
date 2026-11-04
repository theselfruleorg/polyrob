"""Controller.absent_tool_ids: the ids the skill index treats as absent.

A ``leaf-blocked`` tool is one a delegated child never holds, so a skill whose every
declared tool is leaf-blocked must not be listed on a leaf (skill revalidation LOW-7).
"""
import types

import tools.tool_disclosure as disclosure
from tools.controller.tool_management import ToolManagementMixin


def _controller(is_leaf: bool):
    return types.SimpleNamespace(list_tools=lambda: [], container=None,
                                 _is_sub_agent=is_leaf)


def _patch(monkeypatch, reasons):
    monkeypatch.setattr(disclosure, "disclosure_ceiling", lambda c: None)
    monkeypatch.setattr(disclosure, "seat_probe", lambda c: None)

    def fake(tool_id, **kw):
        status, reason = reasons[tool_id]
        return disclosure.ToolStatus(tool_id, status, reason, "")
    monkeypatch.setattr(disclosure, "resolve_tool_status", fake)


def test_a_leaf_blocked_tool_counts_as_absent(monkeypatch):
    _patch(monkeypatch, {
        "github": ("gated", "leaf-blocked"),
        "mcp": ("gated", "unavailable-on-this-deploy"),
        "defi_trade": ("gated", "money"),          # reachable through a card: present
        "filesystem": ("loaded", ""),
    })
    got = ToolManagementMixin.absent_tool_ids(
        _controller(is_leaf=True), ["github", "mcp", "defi_trade", "filesystem"])
    assert got == {"github", "mcp"}
