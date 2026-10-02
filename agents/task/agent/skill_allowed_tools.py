"""``allowed-tools`` frontmatter — ADVISORY only (067 P6, plan T4.2).

agentskills.io lets a SKILL.md declare ``allowed-tools`` (a space-separated
string). The proposal asks to honour it as a NARROWING filter on the session
when the skill loads. There is no clean per-turn tool-narrowing hook in the
controller today (the action set is fixed at registration; the only narrowing
is ``delegation.narrow_child_tools`` at a child's spawn), and 067 does not add a
second controller mechanism. So the declaration is:

* recorded — ``SkillManager.skill_allowed_tools[<cache key>]`` and a log line;
* surfaced — one advisory line appended to the body that ``load_skill`` (and the
  eager ``<skills>`` injection) returns.

It is NOT enforced. The names are the skill author's (often Claude Code tool
names such as ``Bash Read``), not POLYROB tool ids.
"""
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_MAX_TOOLS = 64


def parse_allowed_tools(meta: Dict[str, Any]) -> List[str]:
    raw = (meta or {}).get("allowed-tools")
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    elif isinstance(raw, str):
        items = raw.replace(",", " ").split()
    else:
        return []
    out: List[str] = []
    for t in items:
        t = t.strip()
        if t and t not in out and len(t) <= 128:
            out.append(t)
    return out[:_MAX_TOOLS]


def advisory_line(tools: List[str]) -> str:
    return ("[allowed-tools, advisory] This skill declares it uses only: "
            + " ".join(tools)
            + ". POLYROB does not enforce this list; prefer these tools while you follow it.")


def apply(meta: Dict[str, Any], body: str, *, skill_id: str = "") -> Tuple[str, List[str]]:
    """``(body_with_advisory, tools)``; the body is unchanged when the skill
    declares no ``allowed-tools``."""
    tools = parse_allowed_tools(meta)
    if not tools:
        return body, []
    logger.info("skill %s declares allowed-tools (advisory, not enforced): %s",
                skill_id or "?", " ".join(tools))
    return f"{body.rstrip()}\n\n{advisory_line(tools)}\n", tools
