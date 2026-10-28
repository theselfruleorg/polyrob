"""093 D1 — the ``harness`` status slot: which tools are gated, from the owner's seat.

The agent reads its gated tools through ``agent_status``
(``tools.harness_view.gated_tools_block``, over its own session). The owner's
``/status`` (Telegram, REPL, console, doctor) renders the ONE snapshot
(``core.status_snapshot``), and core may not import ``tools`` — so this module
fills the ``harness`` slot of that snapshot (``core.status_sections``) from the
tools tier. ``tools/__init__`` registers it and hands it the block reader:
this module imports only ``core``, so it never joins the tools import cycle
(``tests/test_layering_ratchet.py``).

It does not decide a status: it reads ``gated_tools_block`` over an owner-seat
view of the live container (nothing loaded yet, not a leaf, no autonomous
ceiling), so the slot and the agent's view read the same resolver. Rows are
grouped by (reason, who, when) to keep the render short; a gate that is policy
(``money``, ``autonomous-ceiling``) is not a deploy problem and comes last, on
one line per reason. A row this module cannot parse renders verbatim.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Tuple

from core.status_sections import SectionContext, register_status_section

SLOT = "harness"

#: Gates that are policy, not a deploy problem: shown compactly, last.
POLICY_REASONS = ("money", "autonomous-ceiling")

NO_CONTAINER = ("not read here — no running agent in this process "
                "(chat /status and agent_status read it)")
ALL_USABLE = "all tools usable"

_ROW = re.compile(r"^(?P<tools>.+?): gated:(?P<reason>[^ ]+) · who=(?P<who>[^·]+?) · "
                  r"when=(?P<when>.+)$")
_MONEY_TOOLS = re.compile(r"^money tools \((?P<ids>.*)\)$")


class _OwnerSeat:
    """The controller facts ``gated_tools_block`` reads, for the owner's seat:
    the live container, nothing loaded in a session, not a leaf, no session
    (so no autonomous ceiling)."""
    _is_sub_agent = False
    session_id = None

    def __init__(self, container: Any) -> None:
        self.container = container

    def list_tools(self) -> List[str]:
        return []


def _group(rows: List[str]) -> Tuple[List[str], int]:
    """``(lines, n_tools)``: one line per (reason, who, when), problems first."""
    groups: Dict[Tuple[str, str, str], List[str]] = {}
    verbatim: List[str] = []
    for row in rows:
        m = _ROW.match(row)
        if m is None:
            verbatim.append(row)
            continue
        tools = m.group("tools").strip()
        mm = _MONEY_TOOLS.match(tools)
        ids = [t.strip() for t in mm.group("ids").split(",")] if mm else [tools]
        key = (m.group("reason"), m.group("who").strip(), m.group("when").strip())
        groups.setdefault(key, []).extend(i for i in ids if i)
    problems, policy = [], []
    for (reason, who, when), ids in groups.items():
        line = f"{', '.join(ids)}: {reason} · who={who} · when={when}"
        (policy if reason in POLICY_REASONS else problems).append(line)
    n = sum(len(ids) for ids in groups.values()) + len(verbatim)
    return problems + verbatim + policy, n


def harness_section(ctx: SectionContext, gated_block: Callable[[Any], str]):
    """The ``harness`` Section from ``gated_block(controller) -> str`` (the
    ``tools.harness_view.gated_tools_block`` shape). A raise becomes
    ``unavailable (<reason>)`` through ``core.status_sections.build_slot``
    (reported, never omitted)."""
    from core.status_snapshot import Section
    if ctx.container is None:
        return Section(name=SLOT, lines=[NO_CONTAINER], data={"read": False})
    block = gated_block(_OwnerSeat(ctx.container))
    rows = [ln[2:].strip() for ln in (block or "").splitlines() if ln.startswith("- ")]
    if not rows:
        return Section(name=SLOT, lines=[ALL_USABLE], data={"read": True, "gated": 0})
    lines, n = _group(rows)
    head = f"{n} tool(s) gated (reason · who can fix it · when the fix applies)"
    return Section(name=SLOT, lines=[head] + lines, data={"read": True, "gated": n})


def register(gated_block: Callable[[Any], str]) -> None:
    """Fill the ``harness`` slot with :func:`harness_section` over *gated_block*."""
    register_status_section(SLOT, lambda ctx: harness_section(ctx, gated_block),
                            tenant=False, source="tools.status_harness")


__all__ = ["ALL_USABLE", "NO_CONTAINER", "POLICY_REASONS", "harness_section", "register"]
