"""WS-A capability gate (Fusion HIGH/Q9): when the latest input is untrusted
correspondent DATA, high-impact tools are blocked (owner confirmation required).

Correspondent isolation is prompt-level (a user-role HumanMessage with framing); this
hook is the STRUCTURAL backstop so a successful injection can't drive money/send/
code-exec/delegation tools.
"""
from agents.task.agent.core.correspondent_gate import (
    HIGH_IMPACT_TOOLS,
    build_tool_resolver,
    is_high_impact,
    is_high_impact_call,
    make_correspondent_gate_hook,
)


def test_high_impact_set_covers_the_dangerous_tools():
    for name in ("code_execution", "x402_pay", "hyperliquid", "polymarket",
                 "delegate_task", "email", "twitter", "browser", "coding", "cronjob"):
        assert is_high_impact(name), name


def test_low_impact_tools_pass():
    for name in ("filesystem", "task", "perplexity", "session_search", "send_message"):
        assert not is_high_impact(name)


def test_crypto_read_actions_are_not_high_impact():
    # The pre-hook receives the bare ACTION name. Read actions must never be blocked,
    # so a correspondent-tainted session can still answer "what's the price/history?".
    # Regression: the "trade" substring used to falsely flag get_trade_history.
    for name in ("get_trade_history", "get_order_history", "get_market_details",
                 "get_open_orders", "get_all_positions", "get_funding_rate",
                 "get_current_price", "get_orderbook"):
        assert not is_high_impact(name), name


def test_crypto_trade_actions_are_high_impact():
    # The real money-moving action names must be gated by name (the tool_id is never
    # passed as action_name, so name-level coverage is what actually blocks a trade).
    for name in ("place_limit_order", "place_market_order", "cancel_order",
                 "cancel_all_orders", "update_leverage", "approve_agent",
                 "revoke_agent"):
        assert is_high_impact(name), name


def test_gate_blocks_high_impact_when_tainted():
    tainted = {"v": True}
    hook = make_correspondent_gate_hook(lambda: tainted["v"])
    reason = hook("x402_pay", {}, None)
    assert reason and "correspondent" in reason.lower()


def test_gate_allows_high_impact_when_not_tainted():
    hook = make_correspondent_gate_hook(lambda: False)
    assert hook("x402_pay", {}, None) is None


def test_message_action_is_high_impact_by_name():
    assert is_high_impact("message")


def test_gate_blocks_message_action_when_tainted():
    hook = make_correspondent_gate_hook(lambda: True)
    reason = hook("message", {}, None)
    assert reason and "correspondent" in reason.lower()


def test_gate_allows_message_action_when_not_tainted():
    hook = make_correspondent_gate_hook(lambda: False)
    assert hook("message", {}, None) is None


def test_gate_allows_low_impact_even_when_tainted():
    hook = make_correspondent_gate_hook(lambda: True)
    assert hook("filesystem", {}, None) is None


def test_gate_fails_closed_if_taint_probe_raises():
    def _boom():
        raise RuntimeError("taint probe down")
    hook = make_correspondent_gate_hook(_boom)
    # a high-impact tool must be DENIED if we can't prove the session is untainted
    assert hook("code_execution", {}, None) is not None
    # a low-impact tool is still allowed
    assert hook("filesystem", {}, None) is None


# ---------------------------------------------------------------------------
# Tool-id resolution (the real fix): the pre-hook receives the bare ACTION name
# (run_code, goal_create, x402_fetch, …), NOT the tool_id. Blocking by tool_id
# token alone was dead. is_high_impact_call resolves the action's owning tool_id
# and blocks its high-impact verbs.
# ---------------------------------------------------------------------------

# (action_name, owning_tool_id) pairs that MUST be blocked — these were all
# bypassing the gate because the denylist listed the tool_id, never the verb.
_BLOCKED_CALLS = [
    ("run_code", "code_execution"),          # arbitrary code execution
    ("apply_patch", "coding"),               # repo mutation
    ("str_replace", "coding"),
    ("run_tests", "coding"),                 # runs code
    ("delete_file", "coding"),
    ("cronjob_schedule", "cronjob"),         # durable recurring runs
    ("cronjob_cancel", "cronjob"),
    ("goal_create", "goal"),                 # durable autonomous work
    ("goal_cancel", "goal"),
    ("objective_add", "goal"),
    ("x402_fetch", "x402_pay"),              # the REAL auto-paying action
    ("twitter_post", "twitter"),             # outbound comms
    ("twitter_reply", "twitter"),
    ("go_to_url", "browser"),                # SSRF / exfil
    ("click_element", "browser"),
    ("extract_page_content", "browser"),
    ("execute_tool", "mcp"),                 # MCP dispatch
    ("anysite_execute", "mcp"),              # DYNAMIC MCP direct action — only
    ("gmail_send_email", "mcp"),             # reachable via tool_id resolution
    ("git_commit", "git"),                   # git write verbs beyond push
    ("git_clone", "git"),
    ("fetch_url", "web_fetch"),              # outbound fetch (exfil/SSRF)
]


def test_high_impact_call_blocks_verbs_via_tool_id():
    for action_name, tool_id in _BLOCKED_CALLS:
        assert is_high_impact_call(action_name, tool_id), f"{action_name} ({tool_id})"


def test_high_impact_call_allows_crypto_reads():
    # Crypto (hyperliquid/polymarket) keeps its deliberate read carve-out so a
    # tainted session can still answer "what's the price/history?".
    for action_name in ("get_current_price", "get_trade_history", "get_open_orders",
                        "get_all_positions", "get_orderbook", "get_funding_rate"):
        assert not is_high_impact_call(action_name, "hyperliquid"), action_name
        assert not is_high_impact_call(action_name, "polymarket"), action_name


def test_high_impact_call_blocks_crypto_trades_by_name():
    for action_name in ("place_limit_order", "place_market_order", "cancel_order",
                        "update_leverage", "approve_agent"):
        assert is_high_impact_call(action_name, "hyperliquid"), action_name
        assert is_high_impact_call(action_name, "polymarket"), action_name


def test_high_impact_call_allows_low_impact_tool_reads():
    # Genuinely low-impact tools stay allowed regardless of resolution.
    for action_name, tool_id in (("read_file", "filesystem"),
                                 ("done", None),
                                 ("send_message", None),
                                 ("session_search", None)):
        assert not is_high_impact_call(action_name, tool_id), action_name


def test_high_impact_call_without_tool_id_falls_back_to_name():
    # Backward-compatible: with no resolver, decision matches the name-only path.
    assert is_high_impact_call("delegate_task", None)
    assert is_high_impact_call("place_limit_order", None)
    assert not is_high_impact_call("run_code", None)  # name-only can't know its tool


def test_gate_hook_uses_resolver_to_block_dead_denylist_verbs():
    # The wired hook (construction.py passes a resolver) must block the verbs the
    # old tool_id-only denylist silently allowed.
    resolve = {"run_code": "code_execution", "goal_create": "goal",
               "x402_fetch": "x402_pay", "anysite_execute": "mcp"}.get
    hook = make_correspondent_gate_hook(lambda: True, resolve_tool=resolve)
    for name in ("run_code", "goal_create", "x402_fetch", "anysite_execute"):
        reason = hook(name, {}, None)
        assert reason and "correspondent" in reason.lower(), name


def test_gate_hook_resolver_allows_crypto_reads_when_tainted():
    resolve = {"get_current_price": "hyperliquid"}.get
    hook = make_correspondent_gate_hook(lambda: True, resolve_tool=resolve)
    assert hook("get_current_price", {}, None) is None


class _FakeAction:
    def __init__(self, tool):
        self.tool = tool


class _FakeController:
    def __init__(self, mapping):
        self._mapping = mapping

    def get_action_details(self, name):
        return self._mapping.get(name)


def test_build_tool_resolver_maps_action_to_tool_id():
    ctl = _FakeController({"run_code": _FakeAction("code_execution"),
                           "anysite_execute": _FakeAction("mcp")})
    resolve = build_tool_resolver(ctl)
    assert resolve("run_code") == "code_execution"
    assert resolve("anysite_execute") == "mcp"


def test_build_tool_resolver_returns_none_for_unknown_action():
    resolve = build_tool_resolver(_FakeController({}))
    assert resolve("nope") is None


def test_build_tool_resolver_never_raises():
    class _Boom:
        def get_action_details(self, name):
            raise RuntimeError("registry down")
    resolve = build_tool_resolver(_Boom())
    assert resolve("run_code") is None  # swallowed, degrades to None
    # a None controller yields a resolver that always returns None
    assert build_tool_resolver(None)("run_code") is None


def test_gate_hook_resolver_fault_fails_closed_for_untrusted_names():
    # A raising resolver must not open a hole: the name-level check still applies,
    # and resolution errors are swallowed (treated as unknown tool -> name-only).
    def _boom(_name):
        raise RuntimeError("registry down")
    hook = make_correspondent_gate_hook(lambda: True, resolve_tool=_boom)
    # name-level high-impact still blocked despite resolver fault
    assert hook("delegate_task", {}, None) is not None
    # a plain name that only resolution would catch degrades to allowed (name-only),
    # but the resolver fault itself must never raise out of the hook
    assert hook("filesystem", {}, None) is None
