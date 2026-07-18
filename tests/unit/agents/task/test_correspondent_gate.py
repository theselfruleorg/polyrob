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


def test_compute_posture_verbs_are_name_high_impact():
    # shell + self_env + process verbs enumerated by NAME (not just tool-id), so a
    # tainted session can't reach them even if the tool-id resolver faults.
    for name in ("shell_run", "self_env_install_dep", "self_env_patch_source",
                 "self_env_restart_service", "self_env_git_pull", "self_env_read_source",
                 "process_kill", "process_log", "process_poll", "process_list"):
        assert is_high_impact(name), name


def test_low_impact_tools_pass():
    # P1-4: perplexity/anysite were moved to HIGH-impact (outbound egress = exfil
    # channel, parity with web_fetch/browser), so they are no longer in this list.
    for name in ("filesystem", "task", "session_search", "send_message"):
        assert not is_high_impact(name)


def test_egress_and_money_verbs_are_high_impact():
    # P1-4: money (x402_request) + outbound-egress (anysite/perplexity) + code-exec
    # (run_code, name-parity with shell_run) + curated-memory write must be gated so a
    # correspondent-tainted session can't mint payments, exfil via query params, run
    # code, or persist injection into future prompts.
    for name in ("x402_request", "anysite_api", "perplexity_search", "run_code",
                 "memory", "anysite", "perplexity", "x402_invoice"):
        assert is_high_impact(name), name


def test_egress_money_verbs_blocked_via_tool_id_resolution():
    # Same coverage through the full is_high_impact_call path (name + owning tool_id).
    assert is_high_impact_call("x402_request", "x402_invoice")
    assert is_high_impact_call("anysite_api", "anysite")
    assert is_high_impact_call("perplexity_search", "perplexity")
    assert is_high_impact_call("run_code", "code_execution")


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


def test_agent_status_action_is_high_impact_by_name():
    # I-6 follow-up: agent_status is read-only but reveals wallet balance +
    # tenant ledger — the same data the taint gate deliberately blocks via
    # x402_pay/x402_invoice tool-id membership. Registered directly (no owning
    # tool_id), so it must be enumerated by NAME like message/skill_manage.
    assert is_high_impact("agent_status")


def test_gate_blocks_agent_status_when_tainted():
    hook = make_correspondent_gate_hook(lambda: True)
    reason = hook("agent_status", {}, None)
    assert reason and "correspondent" in reason.lower()


def test_gate_allows_agent_status_when_not_tainted():
    hook = make_correspondent_gate_hook(lambda: False)
    assert hook("agent_status", {}, None) is None


def test_gate_allows_low_impact_even_when_tainted():
    hook = make_correspondent_gate_hook(lambda: True)
    assert hook("filesystem", {}, None) is None


def test_hf_deploy_verbs_are_high_impact():
    # hf_deploy publishes/deletes a PUBLIC HF Space — enumerated by NAME (parity
    # with shell_run/run_code) AND by tool_id, so a resolver fault can't let a
    # tainted session ship or tear down a Space.
    for name in ("deploy", "undeploy", "hf_deploy"):
        assert is_high_impact(name), name
    assert is_high_impact_call("deploy", "hf_deploy")
    assert is_high_impact_call("undeploy", "hf_deploy")


def test_gate_blocks_hf_deploy_when_tainted():
    hook = make_correspondent_gate_hook(lambda: True)
    reason = hook("deploy", {}, None)
    assert reason and "correspondent" in reason.lower()


def test_gate_allows_hf_deploy_when_not_tainted():
    hook = make_correspondent_gate_hook(lambda: False)
    assert hook("deploy", {}, None) is None


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


def test_high_impact_blocks_NAMESPACED_crypto_trade_verbs():
    """H10 (2026-07-15): container-tool actions register NAMESPACED
    (polymarket_place_limit_order), and that namespaced name is what reaches the
    gate hook — but the crypto tool_ids are deliberately excluded from
    HIGH_IMPACT_TOOL_IDS so their reads stay allowed. Matching only the bare names
    let the real runtime name slip the gate entirely (a tainted turn could trade)."""
    for name in ("polymarket_place_limit_order", "hyperliquid_place_limit_order",
                 "hyperliquid_place_market_order", "polymarket_place_market_order",
                 "hyperliquid_cancel_order", "hyperliquid_cancel_all_orders",
                 "hyperliquid_update_leverage", "hyperliquid_approve_agent",
                 "hyperliquid_revoke_agent"):
        # name-only path (no tool_id) must already block — the hook can't rely on
        # tool-id resolution for crypto (it's excluded).
        assert is_high_impact(name), name
        assert is_high_impact_call(name, "hyperliquid"), name


def test_namespaced_crypto_read_verbs_stay_allowed():
    for name in ("polymarket_get_orderbook", "hyperliquid_get_positions",
                 "get_trade_history", "polymarket_data", "hyperliquid_data",
                 "hyperliquid_get_open_orders"):
        assert not is_high_impact(name), name


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
    # P1-4: run_code is now name-enumerated (parity with shell_run), so a tool-id
    # resolver fault can no longer open code execution to a tainted session.
    assert is_high_impact_call("run_code", None)


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


# --- D1 (2026-07-13 review): scoped reply-while-tainted exemption -------------
# Every correspondent reply re-taints, taint blocks all outbound comms, and only
# an owner turn clears it — so the agent could never answer the person who wrote
# in. The exemption permits message/send_email to EXACTLY the tainting
# (surface, address), budget-gated, flag-gated (CORRESPONDENT_REPLY_ENABLED).

def _reply_hook(sources, allowed=True):
    return make_correspondent_gate_hook(
        lambda: True,
        get_taint_sources=lambda: sources,
        reply_allowed=(lambda surface, address: allowed),
    )


def test_reply_to_tainting_address_allowed_via_message():
    hook = _reply_hook({("email", "john@acme.com")})
    assert hook("message", {"surface": "email", "target": "John@Acme.com ",
                            "text": "hi"}, None) is None


def test_reply_to_other_address_still_denied():
    hook = _reply_hook({("email", "john@acme.com")})
    assert hook("message", {"surface": "email", "target": "other@x.com",
                            "text": "hi"}, None)


def test_reply_wrong_surface_denied():
    hook = _reply_hook({("email", "john@acme.com")})
    assert hook("message", {"surface": "telegram", "target": "john@acme.com",
                            "text": "hi"}, None)


def test_send_email_exemption_to_tainting_address():
    hook = _reply_hook({("email", "john@acme.com")})
    assert hook("send_email", {"to_email": "john@acme.com", "subject": "re",
                               "body": "b"}, None) is None


def test_send_email_with_cc_never_exempt():
    """cc/bcc could exfiltrate to a third address — the exemption is 1:1 only."""
    hook = _reply_hook({("email", "john@acme.com")})
    assert hook("send_email", {"to_email": "john@acme.com", "cc": "evil@x.com",
                               "subject": "re", "body": "b"}, None)


def test_other_high_impact_tools_stay_denied_despite_exemption():
    hook = _reply_hook({("email", "john@acme.com")})
    assert hook("x402_pay", {}, None)
    assert hook("run_code", {"code": "x"}, None)
    assert hook("delegate_task", {}, None)


def test_reply_denied_when_budget_says_no():
    hook = _reply_hook({("email", "john@acme.com")}, allowed=False)
    assert hook("message", {"surface": "email", "target": "john@acme.com",
                            "text": "hi"}, None)


def test_reply_denied_without_sources():
    hook = make_correspondent_gate_hook(
        lambda: True, get_taint_sources=lambda: set(),
        reply_allowed=lambda s, a: True)
    assert hook("message", {"surface": "email", "target": "john@acme.com",
                            "text": "hi"}, None)


def test_build_reply_allowed_flag_off_denies(monkeypatch):
    from agents.task.agent.core.correspondent_gate import build_reply_allowed
    monkeypatch.delenv("CORRESPONDENT_REPLY_ENABLED", raising=False)
    allowed = build_reply_allowed(lambda: None, lambda: "t1")
    assert allowed("email", "john@acme.com") is False


def test_build_reply_allowed_rounds_budget(monkeypatch, tmp_path):
    from agents.task.agent.core.correspondent_gate import build_reply_allowed
    from core.surfaces.conversations import ConversationStore
    monkeypatch.setenv("CORRESPONDENT_REPLY_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REPLY_MAX_ROUNDS", "2")
    store = ConversationStore(str(tmp_path / "conv.db"))

    class _C:
        def get_service(self, name):
            return store if name == "conversation_store" else None

    allowed = build_reply_allowed(lambda: _C(), lambda: "t1")
    assert allowed("email", "john@acme.com") is True
    store.record_outbound("t1", "email", "john@acme.com", "r1")
    assert allowed("email", "john@acme.com") is True
    store.record_outbound("t1", "email", "john@acme.com", "r2")
    assert allowed("email", "john@acme.com") is False, "rounds budget exhausted"
