"""044 ratchets (spec §9). Source-level pins; they may only tighten."""
import inspect
import re
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: An action name that MUTATES something. A room turn may read and reply; it may
#: not change the world. Applied to every action each default room tool
#: registers, so a tool whose verbs grow a write is caught by the ratchet rather
#: than by a member discovering it (044 C3: `knowledge` was a room tool and its
#: `kb_ingest`/`kb_remove` carried no capability bits, so nothing caught them).
_WRITE_VERB = re.compile(
    r"(ingest|remove|delete|write|set|create|add|post|send|pay|deploy|launch"
    r"|swap|transfer)")

#: Named, reasoned exemptions — full action names only, never a pattern. A verb
#: that looks like a write but touches ONLY this session's own ephemeral state.
_SESSION_LOCAL_WRITES = frozenset({
    # The TODO list of THIS session. It dies with the session, is never read by
    # another run, and is the one thing a room turn must be able to keep notes in.
    "task_todo_add",
    "task_todo_complete",
    # A PRICE QUOTE for a swap. It signs nothing, broadcasts nothing and touches
    # no wallet — the word "swap" is in the name of the thing being priced, not
    # of the action. `defi_trade` is where a swap is EXECUTED, and that tool is
    # `money` (denied outright, pinned by test_no_money_verb_is_reachable...).
    "defi_data_swap_quote",
})

#: Flag-gated tools are absent from ``TOOL_DESCRIPTORS`` unless their env is on,
#: so the ratchet resolves their class directly. Every id it needs must resolve
#: (asserted below) — a room tool the ratchet cannot SEE is a room tool the
#: ratchet does not bound.
_TOOL_CLASS_FALLBACK = {
    "defi_data": ("tools.defi.data_tool", "DefiDataTool"),
    "defi_trade": ("tools.defi.trade_tool", "DefiTradeTool"),
    "knowledge": ("tools.knowledge_ingest", "KnowledgeTool"),
    "x402_pay": ("tools.x402.service", "X402PayTool"),
    "x402_invoice": ("tools.x402.invoice_tool", "X402InvoiceTool"),
    "launchpad": ("tools.launchpad.tool", "LaunchpadTool"),
    "dapp_browser": ("tools.dapp_browser.tool", "DappBrowserTool"),
}


def _tool_class(tool_id: str):
    import tools  # noqa: F401  (registers the flag-gated descriptors that ARE on)
    from tools.descriptors import get_tool_class
    cls = get_tool_class(tool_id)
    if cls is not None:
        return cls
    path = _TOOL_CLASS_FALLBACK.get(tool_id)
    if path is None:
        return None
    import importlib
    try:
        return getattr(importlib.import_module(path[0]), path[1])
    except Exception:
        return None


def _action_names(tool_id: str):
    """The names this tool's actions are REGISTERED under.

    Mirrors ``tools/controller/tool_management.py``'s namespacing rule exactly
    (prefix with the tool id unless the method is already prefixed), because
    that is the name the room gate is asked about at call time."""
    cls = _tool_class(tool_id)
    assert cls is not None, f"room ratchet cannot resolve the class for {tool_id!r}"
    out = []
    for name, fn in inspect.getmembers(cls, predicate=inspect.isfunction):
        if not hasattr(fn, "action_info"):
            continue
        out.append(name if name == tool_id or name.startswith(f"{tool_id}_")
                   else f"{tool_id}_{name}")
    return sorted(out)


def test_room_toolset_never_touches_money_exec_delegate():
    from core.surfaces.room_policy import room_tool_ids
    from core.tool_capabilities import ids_with
    assert not (set(room_tool_ids()) & (ids_with("money") | ids_with("exec") | ids_with("delegate_blocked")))


def test_every_default_room_tool_action_is_a_read(monkeypatch):
    """044 C3: enumerate the ACTIONS, not just the tool ids.

    The capability table bounds a room by TOOL; `knowledge` sat in the default
    room toolset with an empty capability set, so `kb_ingest` (poison the owner's
    KB) and `kb_remove` (with no source: clear the collection) were reachable by
    any member. Capability bits are not enough — every verb a room tool exposes
    is checked by NAME."""
    monkeypatch.delenv("GROUP_TURN_TOOLS", raising=False)
    from core.surfaces.room_policy import DEFAULT_ROOM_TOOLS, is_room_denied_call
    seen = 0
    for tool_id in DEFAULT_ROOM_TOOLS:
        for name in _action_names(tool_id):
            seen += 1
            if name in _SESSION_LOCAL_WRITES:
                assert not is_room_denied_call(name, tool_id), (
                    f"{name!r} is an exempted session-local write but the gate "
                    f"denies it — the exemption is dead")
                continue
            if _WRITE_VERB.search(name):
                # A write verb may exist on a room tool ONLY if the gate refuses
                # it by name (044 C3's own remedy: drop the tool AND deny the
                # actions). Reachable + write is the failure.
                assert is_room_denied_call(name, tool_id), (
                    f"{name!r} is a WRITE verb REACHABLE from a public room "
                    f"(tool {tool_id!r}) — drop the tool from DEFAULT_ROOM_TOOLS "
                    f"or deny the action in ROOM_DENIED_ACTIONS")
    assert seen >= 6, "the ratchet enumerated suspiciously few room actions"


def test_knowledge_tool_is_unreachable_from_a_room():
    """044 C3 regression pin: the whole `kb_*` family stays denied by NAME, so
    re-adding `knowledge` via GROUP_TURN_TOOLS still cannot reach a write."""
    from core.surfaces.room_policy import DEFAULT_ROOM_TOOLS, is_room_denied_call
    assert "knowledge" not in DEFAULT_ROOM_TOOLS
    for name in _action_names("knowledge"):
        assert is_room_denied_call(name, "knowledge"), name
    for name in ("kb_search", "kb_ingest", "kb_list", "kb_remove", "kb_anything_new"):
        assert is_room_denied_call(name, None), name


def test_no_money_verb_is_reachable_from_a_room():
    """044 spec ratchet 4, behavioural: for EVERY money tool, every action it
    registers is DENIED by the room gate hook that construction.py installs.
    Not a source pin — the real hook, the real action names."""
    from core.surfaces.room_policy import make_room_gate_hook
    from core.tool_capabilities import ids_with

    resolved = {tid: _tool_class(tid) for tid in sorted(ids_with("money"))}
    unresolved = [t for t, c in resolved.items() if c is None]
    assert not unresolved, f"room ratchet cannot see money tools: {unresolved}"

    hook = make_room_gate_hook(lambda: True, resolve_tool=lambda n: _owner_of(n, resolved))
    ctx = types.SimpleNamespace(user_id="owner", role="orchestrator")
    checked = 0
    for tool_id in resolved:
        for name in _action_names(tool_id):
            checked += 1
            denial = hook(name, {}, ctx)
            assert denial, f"the room gate ALLOWED the money verb {name!r}"
            assert "group chat" in str(denial).lower()
    assert checked >= 10, "the ratchet enumerated suspiciously few money actions"


def _owner_of(action_name, resolved):
    for tool_id in resolved:
        if action_name == tool_id or action_name.startswith(f"{tool_id}_"):
            return tool_id
    return None


def test_public_profile_gates_every_owner_injector():
    src = (REPO / "agents/task/agent/core").glob("*.py")
    joined = "\n".join(p.read_text() for p in src)
    for needle in ("load_owner_doc(", "_maybe_prefetch_memory", "_maybe_inject_live_health",
                   "_maybe_inject_episodic_digest", "_load_project_context",
                   # 044 I5 / I3: the <environment> block and the autonomous
                   # continuity bridge were the two injectors with no guard.
                   "set_environment_message(", "_maybe_inject_autonomous_continuity"):
        assert needle in joined
    for f in ("construction.py", "memory_prefetch.py", "live_health.py"):
        assert "is_public_session(" in (REPO / "agents/task/agent/core" / f).read_text(), f


def test_owner_only_replies_are_redirected_for_room_keys():
    src = (REPO / "surfaces/telegram/harness.py").read_text()
    assert src.count("owner_only_reply_target(") >= 3


def test_room_publish_is_scrubbed_and_capped():
    src = (REPO / "core/surfaces/message_router.py").read_text()
    assert "scrub_secret_shapes" in src and "may_reply(" in src


def test_group_turn_kind_is_forged():
    src = (REPO / "tools/controller/turn_origin.py").read_text()
    # Quote-style agnostic: the literal is `== 'group'` in the shipped source.
    assert '"group"' in src or "'group'" in src


def test_router_and_surface_profile_fall_back_by_session_key():
    """Fifth pin (task 22, item 3): a session that is never resident in-process
    (a cold room, or a service-goal run that binds to the room's session key
    without ever creating a live orchestrator for it) must still resolve its
    surface/chat identity from the durable chat registry, not go dark. Both
    `MessageRouter.publish` and `surface_profile` fall back to
    `row_from_session_key` when there is no live orchestrator to introspect."""
    router_src = (REPO / "core/surfaces/message_router.py").read_text()
    binding_src = (REPO / "core/surfaces/binding.py").read_text()
    assert "row_from_session_key" in router_src
    assert "row_from_session_key" in binding_src
    assert "def surface_profile(" in binding_src
