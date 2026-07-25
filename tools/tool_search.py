"""Query-based tool discovery (Tier-3 item 1) — the search/describe counterpart
to the ``<tool-catalog>`` + ``load_tool`` rig (``tools/tool_disclosure.py``).

The catalog answers "what tools exist and are they loaded?"; this module answers
"which tool matches this keyword, and how do I reach it?" — the same question at
MCP scale, where the actual inventory is the dozens-to-hundreds of tools behind
the single ``mcp`` tool, not the ~15-entry built-in descriptor set.

Hard lines (all inherited from the rig — see the dynamic-tool-rig handoff):
- ONE SSOT each: status from :func:`tools.tool_disclosure.resolve_tool_status`;
  capabilities from ``core/tool_capabilities.py``; built-in inventory from
  ``tools/descriptors.py``; MCP inventory from the metadata the Controller already
  discovered (``Controller.iter_mcp_tools_metadata``). No second status oracle.
- READ-ONLY and cheap: NO server connect, NO registry mutation, NO schema-cache
  touch. MCP ``input_schema`` is deep-copied at the Controller seam, and this module
  only reads it — never mutates it (the schema-cache/MCP live-object hazards).
- Honesty: money tools are searchable + describable but NEVER shown loadable (the
  description states the explicit-grant gate); a leaf sees the same structured
  ``gated:leaf-blocked`` the rig uses; gated hits always carry the remedy channel;
  a capped result set says so (no silent truncation).

Rides ``TOOL_PROGRESSIVE_DISCLOSURE`` — registered where ``load_tool`` is.
"""
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from tools.tool_disclosure import _one_liner, resolve_tool_status

_STATUS_RANK = {"loaded": 0, "loadable": 1, "gated": 2}


@dataclass(frozen=True)
class ToolHit:
    id: str
    where: str            # "builtin" | "mcp:<server>"
    status: str           # loaded | loadable | gated (builtin) | available (mcp)
    reason: str = ""
    remedy: str = ""
    description: str = ""
    server_tool_name: str = ""   # mcp only


@dataclass
class SearchResult:
    results: List[ToolHit]
    total_matched: int
    limit: int


# --- inventory ---------------------------------------------------------------

def _builtin_ids() -> List[str]:
    """The FULL built-in vocabulary: every classified capability id UNION every
    descriptor display id. TOOL_DESCRIPTORS alone holds only flag-materialized
    optionals, so a gated-but-unconstructed tool (e.g. ``shell`` on a plain
    server) would otherwise be un-findable — it must surface as gated, not
    invisible (Phase-A finding D3)."""
    from core.tool_capabilities import TOOL_CAPABILITIES
    from tools.descriptors import TOOL_DESCRIPTORS, get_tool_display_name
    ids = set(TOOL_CAPABILITIES.keys())
    for name in TOOL_DESCRIPTORS:
        ids.add(get_tool_display_name(name))
    return sorted(ids)


def _builtin_description(display: str) -> str:
    from tools.descriptors import TOOL_DESCRIPTORS, get_tool_display_name
    desc = TOOL_DESCRIPTORS.get(display)
    if desc is None:
        for name, d in TOOL_DESCRIPTORS.items():
            if get_tool_display_name(name) == display:
                desc = d
                break
    return _one_liner(desc.description) if desc is not None else ""


# --- ranking (deterministic, no LLM / embeddings) ----------------------------

def _score(query: str, tokens: List[str], hay_id: str, hay_desc: str) -> int:
    qid, ddesc = hay_id.lower(), hay_desc.lower()
    score = 0
    if query and query == qid:
        score += 1000
    if query and qid.startswith(query):
        score += 200
    for tok in tokens:
        if tok in qid:
            score += 50
        elif tok in ddesc:
            score += 10
    return score


def search_tools(query: str, *, container, loaded_ids, is_leaf: bool = False,
                 mcp_tools: Optional[List[Dict[str, Any]]] = None,
                 limit: int = 8) -> SearchResult:
    """Rank every callable tool (built-ins + connected MCP tools) against *query*.

    Deterministic: id-exact > id-prefix > id-token > desc-token; ties broken by
    status rank (loaded < loadable < gated) then id ascending. ``total_matched``
    is the pre-cap count so the caller can disclose truncation.
    """
    q = (query or "").strip().lower()
    tokens = [t for t in q.split() if t]
    loaded = set(loaded_ids or ())
    scored: List[tuple] = []  # (-score, status_rank, id, ToolHit)

    for display in _builtin_ids():
        d = _builtin_description(display)
        s = _score(q, tokens, display, d)
        if s <= 0:
            continue
        st = resolve_tool_status(display, container=container,
                                 loaded_ids=loaded, is_leaf=is_leaf)
        scored.append((-s, _STATUS_RANK.get(st.status, 3), display,
                       ToolHit(display, "builtin", st.status, st.reason, st.remedy, d)))

    for m in (mcp_tools or []):
        name = m.get("name", "") or ""
        server = m.get("server", "") or ""
        stn = m.get("server_tool_name", "") or name
        desc = m.get("description", "") or ""
        s = _score(q, tokens, name, desc)
        if s <= 0:
            continue
        remedy = (f'call mcp_execute_tool(server_name="{server}", '
                  f'tool_name="{stn}", arguments={{...}})')
        scored.append((-s, 0, name,
                       ToolHit(name, f"mcp:{server}", "available", "", remedy,
                               _one_liner(desc), stn)))

    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    total = len(scored)
    n = max(1, int(limit))
    return SearchResult([t[3] for t in scored[:n]], total, n)


# --- ActionResult wrappers (thin closures delegate here, like perform_load_tool) --

def _is_leaf(controller, execution_context) -> bool:
    return bool(
        getattr(execution_context, "is_sub_agent", False)
        or getattr(execution_context, "role", None) == "leaf"
        or getattr(controller, "_is_sub_agent", False))


def _mcp_metadata(controller) -> List[Dict[str, Any]]:
    getter = getattr(controller, "iter_mcp_tools_metadata", None)
    if not callable(getter):
        return []
    try:
        return getter() or []
    except Exception:
        return []


def _hit_line(h: ToolHit) -> str:
    if h.where == "builtin":
        if h.status == "loaded":
            tag = "[loaded]"
        elif h.status == "loadable":
            tag = f'[loadable — load_tool("{h.id}")]'
        else:
            tag = f"[gated:{h.reason} — {h.remedy}]"
        loc = "builtin"
    else:
        tag = "[available via mcp]"
        loc = h.where
    return f"- {h.id} ({loc}): {h.description} {tag}".rstrip()


async def perform_tool_search(controller, query: str, limit: int = 8,
                              execution_context=None):
    """The ``tool_search`` action. Read-only; safe for leaves (shows honest
    per-tool status, never refuses the search itself)."""
    from tools.controller.types import ActionResult

    sr = search_tools(query, container=getattr(controller, "container", None),
                      loaded_ids=set(controller.list_tools()),
                      is_leaf=_is_leaf(controller, execution_context),
                      mcp_tools=_mcp_metadata(controller), limit=limit)
    if not sr.results:
        return ActionResult(
            extracted_content=(
                f"tool_search('{query}'): no matching tools. Browse the "
                "<tool-catalog> for built-ins, or load_tool('mcp') to connect "
                "MCP servers and discover their tools."),
            include_in_memory=True)
    lines = [f"tool_search('{query}') — {sr.total_matched} match(es):"]
    lines += [_hit_line(h) for h in sr.results]
    if sr.total_matched > len(sr.results):
        lines.append(
            f"(showing {len(sr.results)} of {sr.total_matched}; refine the query "
            "or raise limit. Use tool_describe('<id>') for full detail.)")
    else:
        lines.append("Use tool_describe('<id>') for full detail.")
    return ActionResult(extracted_content="\n".join(lines), include_in_memory=True)


def describe_tool(tool_id: str, *, container, loaded_ids, is_leaf: bool,
                  mcp_tools: List[Dict[str, Any]], registry) -> str:
    """Render the full detail block for one id. Pure over its inputs; the wrapper
    supplies them from the controller."""
    from core.tool_capabilities import TOOL_CAPABILITIES
    from tools.descriptors import TOOL_DESCRIPTORS, get_tool_display_name

    raw = (tool_id or "").strip().strip('"').lower()
    display = get_tool_display_name(raw)

    # MCP tool? Match the canonical {server}_{tool} name (read-only metadata).
    # Case-insensitive: MCP tool names are external/uncontrolled, and the agent
    # copies the id back from tool_search verbatim.
    for m in (mcp_tools or []):
        if (m.get("name") or "").lower() == raw:
            schema = json.dumps(m.get("input_schema") or {}, indent=2, sort_keys=True)
            return "\n".join([
                f"tool_describe('{m.get('name')}') — MCP tool",
                f"server: {m.get('server')}",
                f"description: {m.get('description') or '(none)'}",
                "status: available (call it now via mcp_execute_tool)",
                'invoke: mcp_execute_tool(server_name="%s", tool_name="%s", arguments={...})'
                % (m.get("server"), m.get("server_tool_name") or m.get("name")),
                "input_schema:", schema,
            ])

    if display not in TOOL_CAPABILITIES and display not in TOOL_DESCRIPTORS:
        return (f"tool_describe('{raw}'): unknown tool id. Use "
                "tool_search('<keyword>') or browse the <tool-catalog>.")

    st = resolve_tool_status(display, container=container,
                             loaded_ids=set(loaded_ids or ()), is_leaf=is_leaf)
    caps = TOOL_CAPABILITIES.get(display)
    cap_line = ("capabilities: (unclassified)" if caps is None
                else "capabilities: " + (", ".join(sorted(caps)) if caps else "(none)"))
    lines = [f"tool_describe('{display}') — built-in tool",
             f"description: {_builtin_description(display) or '(none)'}",
             cap_line]
    desc = TOOL_DESCRIPTORS.get(display)
    req = list(getattr(desc, "required_config", None) or []) if desc else []
    if req:
        lines.append("required_config: " + ", ".join(req))

    if st.status == "loaded":
        lines.append("status: loaded — its actions are callable now")
        actions = {}
        if registry is not None and hasattr(registry, "get_actions_by_service"):
            try:
                actions = registry.get_actions_by_service(display) or {}
            except Exception:
                actions = {}
        if actions:
            lines.append("actions:")
            for name in sorted(actions):
                pm = getattr(actions[name], "param_model", None)
                try:
                    schema = pm.model_json_schema() if pm is not None else {}
                    props = ", ".join(sorted((schema.get("properties") or {}).keys())) \
                        or "(no params)"
                except Exception:
                    props = "(schema unavailable)"
                lines.append(f"  - {name}({props})")
    elif st.status == "loadable":
        lines.append(f'status: loadable — call load_tool("{display}") first, then '
                     "its actions appear next step")
    else:
        lines.append(f"status: gated:{st.reason} — {st.remedy}")
    return "\n".join(lines)


async def perform_tool_describe(controller, tool_id: str, execution_context=None):
    """The ``tool_describe`` action. Read-only; safe for leaves."""
    from tools.controller.types import ActionResult
    content = describe_tool(
        tool_id,
        container=getattr(controller, "container", None),
        loaded_ids=set(controller.list_tools()),
        is_leaf=_is_leaf(controller, execution_context),
        mcp_tools=_mcp_metadata(controller),
        registry=getattr(controller, "registry", None))
    return ActionResult(extracted_content=content, include_in_memory=True)
