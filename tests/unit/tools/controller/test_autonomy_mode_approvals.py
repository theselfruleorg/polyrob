"""013 T4 — act-and-report approvals under AUTONOMY_MODE=autonomous.

Under effective autonomous mode (`full_autonomy_enabled()`), the generic approval
seam defaults to the new `auto_notify` provider (allow + audit + post-hoc owner
notify — the generic analog of PAYMENT_APPROVAL_MODE=auto), EXCEPT an
always-owner-queued lane: self-modification verbs (`_ALWAYS_GATED_VERBS`) and
owner `approvals.require` pref pins keep the durable `owner_queue` provider.
Supervised mode (AUTONOMY_MODE unset) stays byte-identical.

Patch env via monkeypatch — never importlib.reload (rebinds AutonomyConfig).
"""
import os
import types

import pytest

import agents.task.constants as constants
import tools.controller.approval as approval
import tools.controller.approval_queue as approval_queue  # noqa: F401 — pre-import so
# its module-level register_approval_provider("owner_queue", ...) has ALREADY run
# before any test monkeypatches approval._PROVIDERS["owner_queue"] (mirrors
# tests/unit/tools/controller/test_payment_approval_mode.py).
from core.prefs import write_preference
from tools.controller.types import ActionResult


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "APPROVAL_REQUIRED_TOOLS", "APPROVAL_PROVIDER",
              "POLYROB_TOOL_DENYLIST", "PAYMENT_APPROVAL_MODE",
              "AUTONOMY_MODE", "POLYROB_LOCAL", "ROB_LOCAL", "POLYROB_OWNER_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()
    constants._refreeze_payment_approval_flags_for_tests()
    yield
    # Refreeze from clean env: monkeypatch teardown runs AFTER this fixture's teardown,
    # so we must delenv first to prevent test-set values from polluting frozen globals.
    for k in ("AGENT_COMPUTE_POSTURE", "APPROVAL_REQUIRED_TOOLS", "APPROVAL_PROVIDER",
              "POLYROB_TOOL_DENYLIST", "PAYMENT_APPROVAL_MODE",
              "AUTONOMY_MODE", "POLYROB_LOCAL", "ROB_LOCAL", "POLYROB_OWNER_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()
    constants._refreeze_payment_approval_flags_for_tests()


def _enable_full(monkeypatch):
    """Effective autonomous mode: mode + local + bound owner (see T1's helper in
    tests/unit/agents/task/test_autonomy_mode.py)."""
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    constants.reset_autonomy_mode_warnings()


def _posture2(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    constants._refreeze_compute_posture_for_tests()


def _make_controller(tmp_path, user_id="rob", tainted=False):
    import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
    from tools.controller.service import Controller

    orch = types.SimpleNamespace(
        session_id="s1", user_id=user_id, workspace_dir=str(tmp_path),
        _correspondent_tainted=tainted,
    )
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    return Controller(container=container, orchestrator=orch)


class _SpyProvider(approval.ApprovalProvider):
    calls = []
    outcome = True

    async def request(self, action_name, params, context):
        _SpyProvider.calls.append((action_name, dict(params or {})))
        return _SpyProvider.outcome


@pytest.fixture(autouse=True)
def _reset_spy():
    _SpyProvider.calls = []
    _SpyProvider.outcome = True


# --- provider registration ------------------------------------------------------


def test_auto_notify_provider_registered():
    provider = approval.get_approval_provider("auto_notify")
    assert isinstance(provider, approval.AutoNotifyApprover)


@pytest.mark.asyncio
async def test_auto_notify_provider_allows():
    provider = approval.get_approval_provider(
        "auto_notify", user_id="rob", home_dir="/tmp")
    assert await provider.request("shell_run", {"cmd": "ls"}, None) is True


# --- provider default under autonomous mode --------------------------------------


def test_autonomous_mode_defaults_provider_auto_notify(monkeypatch, tmp_path):
    _enable_full(monkeypatch)
    _posture2(monkeypatch)  # would default interactive_cli today
    _gates, provider = approval.effective_approval_state("rob", tmp_path)
    assert provider == "auto_notify"


def test_autonomous_mode_explicit_provider_still_wins(monkeypatch, tmp_path):
    """An explicit env APPROVAL_PROVIDER (deny here) is never loosened by the mode."""
    _enable_full(monkeypatch)
    _posture2(monkeypatch)
    monkeypatch.setenv("APPROVAL_PROVIDER", "deny")
    approval._refreeze_approval_flags_for_tests()
    _gates, provider = approval.effective_approval_state("rob", tmp_path)
    assert provider == "deny"


def test_supervised_mode_unchanged(monkeypatch, tmp_path):
    """AUTONOMY_MODE unset -> posture-2 default stays interactive_cli (byte-identical)."""
    _posture2(monkeypatch)
    _gates, provider = approval.effective_approval_state("rob", tmp_path)
    assert provider == "interactive_cli"


def test_supervised_mode_resolve_gated_actions_unchanged(monkeypatch):
    """Regression: with the mode off, resolve_gated_actions is byte-identical."""
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "git_push")
    approval._refreeze_approval_flags_for_tests()
    actions, provider = approval.resolve_gated_actions()
    assert actions == {"git_push"}
    assert provider == "auto"


# --- the always-owner-queued lane -------------------------------------------------


def test_always_gated_verbs_membership():
    """Verified against real registered action names: the four self_env_* verbs
    (tools/self_env/tool.py) + mcp_install (action_registration.py). self_modify /
    tool_manage are aspirational tokens kept for defense-in-depth (they already sit
    in DEFAULT_APPROVAL_REQUIRED_TOOLS + the correspondent-gate high-impact set)."""
    assert approval._ALWAYS_GATED_VERBS == frozenset({
        "self_modify",
        "self_env_install_dep", "self_env_patch_source",
        "self_env_restart_service", "self_env_git_pull",
        "mcp_install", "tool_manage",
    })


def test_always_gated_verbs_stay_owner_queued(monkeypatch, tmp_path):
    _enable_full(monkeypatch)
    _posture2(monkeypatch)
    gates, provider = approval.effective_approval_state("rob", tmp_path)
    assert provider == "auto_notify"
    queued, reported = approval.autonomous_gating_lanes(gates)
    for verb in ("self_modify", "self_env_install_dep", "self_env_patch_source",
                 "self_env_restart_service", "self_env_git_pull",
                 "mcp_install", "tool_manage"):
        assert verb in queued, verb
        assert verb not in reported, verb
    # shell_run is posture-gated but NOT self-modification — it belongs to the
    # act-and-report lane under autonomous mode (corrections item 5).
    assert "shell_run" in reported and "shell_run" not in queued


def test_owner_pref_pin_still_tightens(monkeypatch, tmp_path):
    """approvals.require additions land in the owner-queued lane even under
    autonomous mode."""
    _enable_full(monkeypatch)
    write_preference(tmp_path, "rob", "approvals.require", ["twitter_post"])
    gates, provider = approval.effective_approval_state("rob", tmp_path)
    assert gates.get("twitter_post") == "pref"
    queued, reported = approval.autonomous_gating_lanes(gates)
    assert "twitter_post" in queued and "twitter_post" not in reported


def test_owner_provider_pref_tightens_over_auto_notify(monkeypatch, tmp_path):
    """approvals.provider=interactive_cli/deny is STRICTER than auto_notify on the
    _PROVIDER_ORDER ladder — the pref wins over the mode default."""
    _enable_full(monkeypatch)
    _posture2(monkeypatch)
    write_preference(tmp_path, "rob", "approvals.provider", "deny")
    _gates, provider = approval.effective_approval_state("rob", tmp_path)
    assert provider == "deny"


# --- the post-execution notify hook (corrections item 6) --------------------------


@pytest.mark.asyncio
async def test_tool_auto_notify_hook_notifies_and_audits(monkeypatch):
    notified = []
    audited = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)
    monkeypatch.setattr(
        "tools.controller.approval_queue._emit_tool_auto_approved",
        lambda *a, **k: audited.append(a))

    hook = approval_queue.make_tool_auto_notify_hook(object(), {"shell_run"})
    ctx = types.SimpleNamespace(user_id="rob", session_id="s1")
    ok = ActionResult(extracted_content="ok")

    await hook("shell_run", {"cmd": "ls"}, ok, ctx)
    assert len(notified) == 1 and len(audited) == 1
    assert notified[0][0] == "rob"
    assert "shell_run" in notified[0][1]
    assert "AUTONOMY_MODE=autonomous" in notified[0][1]


@pytest.mark.asyncio
async def test_tool_auto_notify_hook_skips_ungated_and_errored(monkeypatch):
    notified = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    hook = approval_queue.make_tool_auto_notify_hook(object(), {"shell_run"})
    ctx = types.SimpleNamespace(user_id="rob", session_id="s1")

    await hook("read_file", {}, ActionResult(extracted_content="ok"), ctx)
    await hook("shell_run", {}, ActionResult(error="boom"), ctx)
    assert notified == []


@pytest.mark.asyncio
async def test_tool_auto_notify_hook_suppressed_on_taint(monkeypatch):
    notified = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    hook = approval_queue.make_tool_auto_notify_hook(
        object(), {"shell_run"}, taint_probe=lambda: True)
    ctx = types.SimpleNamespace(user_id="rob", session_id="s1")
    await hook("shell_run", {"cmd": "ls"}, ActionResult(extracted_content="ok"), ctx)
    assert notified == []


@pytest.mark.asyncio
async def test_tool_auto_notify_hook_raising_taint_probe_fails_closed(monkeypatch):
    notified = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    def _boom():
        raise RuntimeError("probe exploded")

    hook = approval_queue.make_tool_auto_notify_hook(
        object(), {"shell_run"}, taint_probe=_boom)
    ctx = types.SimpleNamespace(user_id="rob", session_id="s1")
    await hook("shell_run", {"cmd": "ls"}, ActionResult(extracted_content="ok"), ctx)
    assert notified == []


# --- two-lane Controller wiring (corrections item 7) -------------------------------


@pytest.mark.asyncio
async def test_two_lane_wiring_queued_verb_routes_owner_queue(tmp_path, monkeypatch):
    _enable_full(monkeypatch)
    _posture2(monkeypatch)
    approval._refreeze_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)

    c = _make_controller(tmp_path)
    reason = await c._run_pre_tool_call_hooks("self_env_patch_source", {}, None)

    assert reason is None  # the spy (owner_queue lane) approved
    assert ("self_env_patch_source", {}) in _SpyProvider.calls


@pytest.mark.asyncio
async def test_two_lane_wiring_queued_verb_denied_when_owner_queue_denies(
        tmp_path, monkeypatch):
    _enable_full(monkeypatch)
    _posture2(monkeypatch)
    approval._refreeze_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)
    _SpyProvider.outcome = False

    c = _make_controller(tmp_path)
    reason = await c._run_pre_tool_call_hooks("self_env_patch_source", {}, None)

    assert reason is not None and "self_env_patch_source" in reason


@pytest.mark.asyncio
async def test_two_lane_wiring_reported_verb_allowed_and_notifies(tmp_path, monkeypatch):
    _enable_full(monkeypatch)
    _posture2(monkeypatch)
    approval._refreeze_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)
    notified = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    c = _make_controller(tmp_path)

    # Pre: allowed by auto_notify (act-and-report), never consults owner_queue.
    reason = await c._run_pre_tool_call_hooks("shell_run", {"cmd": "ls"}, None)
    assert reason is None
    assert _SpyProvider.calls == []

    # Post: one owner notification for the successful run.
    ctx = types.SimpleNamespace(user_id="rob", session_id="s1")
    await c._run_post_tool_call_hooks(
        "shell_run", {"cmd": "ls"}, ActionResult(extracted_content="ok"), ctx)
    assert len(notified) == 1 and "shell_run" in notified[0][1]


def test_payment_tools_excluded_from_generic_reported_lane_wiring(tmp_path, monkeypatch):
    """013 T4 review, Finding 2: PAYMENT_APPROVAL_TOOLS (x402_request et al.)
    already get a first-class pre-hook (PAYMENT_APPROVAL_MODE -> owner_queue) +
    post-hoc notify (mode=auto) of their own — Controller.__init__'s two-lane
    auto_notify wiring must never ALSO gate/notify them through the generic
    REPORTED lane, or a within-cap auto payment gets a second, misleading
    "[auto-approved]" line even when the owner explicitly approved it.

    x402_request is DEFAULT_APPROVAL_REQUIRED_TOOLS-gated (approval.py:197), so
    at posture>=2 it lands in the effective gated set and — absent the fix —
    the pure `autonomous_gating_lanes` split would route it to `reported`
    (not `_ALWAYS_GATED_VERBS`/pref-pinned). Assert the ACTUAL wiring excludes
    it from both the auto_notify pre-hook's tools set and the generic notify
    hook's tools set, while a non-payment reported verb (shell_run) is
    unaffected."""
    _enable_full(monkeypatch)
    _posture2(monkeypatch)
    approval._refreeze_approval_flags_for_tests()

    # Precondition: without the Controller-level exclusion, the pure lane split
    # WOULD put x402_request in `reported` — proving the fix is load-bearing.
    gates, provider = approval.effective_approval_state("rob", tmp_path)
    assert provider == "auto_notify"
    assert "x402_request" in gates
    _queued_raw, reported_raw = approval.autonomous_gating_lanes(gates)
    assert "x402_request" in reported_raw
    assert "x402_request" not in _queued_raw  # not _ALWAYS_GATED_VERBS/pref-pinned

    import tools.controller.approval_queue as approval_queue_mod

    pre_hook_calls = []
    orig_make_approval_hook = approval.make_approval_hook

    def _spy_make_approval_hook(prov, required_tools, **kw):
        pre_hook_calls.append((prov, set(required_tools)))
        return orig_make_approval_hook(prov, required_tools, **kw)

    notify_hook_calls = []
    orig_make_notify_hook = approval_queue_mod.make_tool_auto_notify_hook

    def _spy_make_notify_hook(container, tools, taint_probe=None):
        notify_hook_calls.append(set(tools))
        return orig_make_notify_hook(container, tools, taint_probe=taint_probe)

    monkeypatch.setattr(approval, "make_approval_hook", _spy_make_approval_hook)
    monkeypatch.setattr(
        approval_queue_mod, "make_tool_auto_notify_hook", _spy_make_notify_hook)

    _make_controller(tmp_path)

    # The auto_notify (reported-lane) pre-hook is the one wired with an
    # AutoNotifyApprover instance -- disambiguates it from the owner_queue-backed
    # queued-lane / payment-approve pre-hooks also registered via make_approval_hook.
    reported_lane_calls = [
        tools for prov, tools in pre_hook_calls
        if isinstance(prov, approval.AutoNotifyApprover)
    ]
    assert len(reported_lane_calls) == 1
    assert "x402_request" not in reported_lane_calls[0]
    assert "shell_run" in reported_lane_calls[0]  # non-payment verb unaffected

    assert len(notify_hook_calls) == 1
    assert "x402_request" not in notify_hook_calls[0]
    assert "shell_run" in notify_hook_calls[0]


@pytest.mark.asyncio
async def test_payment_tools_generic_lane_does_not_double_notify(tmp_path, monkeypatch):
    """Behavioral proof of Finding 2: with PAYMENT_APPROVAL_MODE=auto, a
    successful x402_request run must fire exactly ONE owner notification (the
    payment-specific auto-notify hook) — not a second, generic-lane
    "[auto-approved]" line."""
    _enable_full(monkeypatch)
    _posture2(monkeypatch)
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    approval._refreeze_approval_flags_for_tests()
    constants._refreeze_payment_approval_flags_for_tests()

    notified = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    c = _make_controller(tmp_path)
    ctx = types.SimpleNamespace(user_id="rob", session_id="s1")
    result = ActionResult(extracted_content="ok", metadata={
        "request_id": "inv_abc123", "amount_usd": 5.0, "purpose": "consulting"})
    await c._run_post_tool_call_hooks("x402_request", {"amount_usd": 5}, result, ctx)

    assert len(notified) == 1, notified
    assert "inv_abc123" in notified[0][1]


@pytest.mark.asyncio
async def test_supervised_wiring_still_single_lane(tmp_path, monkeypatch):
    """Regression: AUTONOMY_MODE unset + explicit deny provider — the single-hook
    supervised wiring denies exactly as before, and no auto-notify post hook fires."""
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "git_push")
    monkeypatch.setenv("APPROVAL_PROVIDER", "deny")
    approval._refreeze_approval_flags_for_tests()
    notified = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    c = _make_controller(tmp_path)
    reason = await c._run_pre_tool_call_hooks("git_push", {}, None)
    assert reason is not None and "git_push" in reason

    ctx = types.SimpleNamespace(user_id="rob", session_id="s1")
    await c._run_post_tool_call_hooks(
        "git_push", {}, ActionResult(extracted_content="ok"), ctx)
    assert notified == []


# --- T7: PAYMENT_APPROVAL_MODE defaults to "auto" under full autonomy -------------
#
# Receive-side only (proposal 013 §1.4/§2.5): the "approve" -> owner_queue path
# hard-denies forged/autonomous turns (approval_queue.py OwnerQueueApprover), so
# autonomous invoicing is impossible under "approve"; "auto" executes within
# X402_INVOICE_MAX_USD/X402_INVOICE_DAILY_MAX + post-hoc owner notify (already
# wired, tested above). The money-SPEND *enablement* flags (x402_pay, wallet,
# HYPERLIQUID_TRADING_ENABLED, etc.) are untouched by AUTONOMY_MODE (pinned below).
# The live-trade ORDER-PLACEMENT verbs in PAYMENT_APPROVAL_TOOLS are a DIFFERENT
# axis (Controller-level pre-approval wiring, not an enablement flag) — see the
# "T7 review fix" section further down for their owner_queue-under-auto coverage.


def test_full_autonomy_unset_payment_mode_defaults_auto(monkeypatch):
    _enable_full(monkeypatch)
    constants._refreeze_payment_approval_flags_for_tests()
    assert constants.payment_approval_mode() == "auto"


def test_full_autonomy_explicit_approve_still_wins(monkeypatch):
    _enable_full(monkeypatch)
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    constants._refreeze_payment_approval_flags_for_tests()
    assert constants.payment_approval_mode() == "approve"


def test_supervised_unset_payment_mode_stays_approve(monkeypatch):
    """Regression: AUTONOMY_MODE unset (or supervised) -> byte-identical default."""
    constants._refreeze_payment_approval_flags_for_tests()
    assert constants.payment_approval_mode() == "approve"


def test_delegate_blocked_tools_money_entries_untouched():
    """Regression pin: leaf-delegation of any money tool_id stays blocked regardless
    of AUTONOMY_MODE (a different axis from the mode's capability/approval grants)."""
    from tools.controller.delegation import DELEGATE_BLOCKED_TOOLS

    for tool_id in ("x402_pay", "x402_invoice", "hyperliquid", "polymarket"):
        assert tool_id in DELEGATE_BLOCKED_TOOLS, tool_id


def test_correspondent_gate_high_impact_money_verbs_untouched():
    """Regression pin: the correspondent-taint high-impact gate still blocks the
    money verbs/tool_ids regardless of AUTONOMY_MODE. hyperliquid/polymarket
    tool_ids are (by design, pre-existing) absent from HIGH_IMPACT_TOOL_IDS so
    their READ verbs stay allowed while tainted — their trade verbs are enumerated
    by name in _HIGH_IMPACT_NAMES instead; assert both money-relevant sets are
    intact rather than assuming they overlap."""
    from agents.task.agent.core.correspondent_gate import (
        HIGH_IMPACT_TOOL_IDS, _HIGH_IMPACT_NAMES,
    )

    # x402_pay/x402_invoice tool_ids are fully high-impact (every action gated).
    for tool_id in ("x402_pay", "x402_invoice"):
        assert tool_id in HIGH_IMPACT_TOOL_IDS, tool_id
    # hyperliquid/polymarket legacy tokens + their trade verbs + the money action
    # names are enumerated by name (name-based layer, not tool_id resolution).
    for name in ("hyperliquid", "polymarket", "x402_pay", "x402_invoice",
                 "x402_request", "place_limit_order", "place_market_order"):
        assert name in _HIGH_IMPACT_NAMES, name


def test_money_spend_flags_default_false_under_full_autonomy_and_not_mode_governed(
        monkeypatch):
    """The five money-SPEND flags (proposal 013 global constraint) keep today's
    default (False) even under full autonomy, and are never members of
    constants._MODE_CAPABILITY_FLAGS — only receive-side (X402_INVOICE_ENABLED) is
    mode-governed."""
    _enable_full(monkeypatch)

    from core.wallet.config import load_wallet_config
    from tools.crypto_trade_gate import evaluate_live_trade
    from tools.x402 import x402_client_enabled

    assert x402_client_enabled() is False
    wallet_cfg = load_wallet_config(os.environ, user_id="rob", home_dir=None)
    assert wallet_cfg.enabled is False

    for venue in ("hyperliquid", "polymarket"):
        decision = evaluate_live_trade(venue, 1.0)
        assert decision.live is False

    money_spend_flags = {
        "X402_CLIENT_ENABLED", "AGENT_WALLET_ENABLED",
        "HYPERLIQUID_TRADING_ENABLED", "POLYMARKET_TRADING_ENABLED",
        "CRYPTO_TRADE_LIVE_ENABLED",
    }
    assert money_spend_flags.isdisjoint(constants._MODE_CAPABILITY_FLAGS)


# --- T7 review fix (Important finding): SPEND verbs keep owner_queue pre-approval
# under mode="auto", even when that mode is the DEFAULT under full autonomy ---------
#
# The original T7 cut defaulted PAYMENT_APPROVAL_MODE to "auto" under full autonomy
# without distinguishing PAYMENT_APPROVAL_TOOLS' receive vs spend members — so the
# four live-trade order verbs (L9 additions) silently lost pre-approval and only got
# a misleading post-hoc "auto-approved" notify, crossing the hard line that trading
# is never act-and-report. These tests prove the Controller wiring: under full
# autonomy's DEFAULTED "auto", the trade verbs are STILL owner_queue pre-approved;
# only x402_request (PAYMENT_RECEIVE_APPROVAL_TOOLS) is act-and-report.

_TRADE_VERBS = (
    "hyperliquid_place_limit_order", "hyperliquid_place_market_order",
    "polymarket_place_limit_order", "polymarket_place_market_order",
)


@pytest.mark.asyncio
async def test_full_autonomy_defaulted_auto_spend_verbs_still_owner_queued(
        tmp_path, monkeypatch):
    _enable_full(monkeypatch)
    constants._refreeze_payment_approval_flags_for_tests()
    assert constants.payment_approval_mode() == "auto"  # the T7 default
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)

    c = _make_controller(tmp_path)
    for verb in _TRADE_VERBS:
        _SpyProvider.calls = []
        reason = await c._run_pre_tool_call_hooks(verb, {"amount_usd": 5}, None)
        assert reason is None, verb  # the spy (owner_queue) approved
        assert (verb, {"amount_usd": 5}) in _SpyProvider.calls, verb


@pytest.mark.asyncio
async def test_full_autonomy_defaulted_auto_spend_verb_denied_when_owner_queue_denies(
        tmp_path, monkeypatch):
    _enable_full(monkeypatch)
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)
    _SpyProvider.outcome = False

    c = _make_controller(tmp_path)
    reason = await c._run_pre_tool_call_hooks(
        "hyperliquid_place_market_order", {"amount_usd": 5}, None)
    assert reason is not None and "hyperliquid_place_market_order" in reason


@pytest.mark.asyncio
async def test_full_autonomy_defaulted_auto_receive_verb_still_act_and_report(
        tmp_path, monkeypatch):
    """x402_request (the receive-side subset) is UNCHANGED by this fix: still not
    queued through owner_queue, still act-and-report."""
    _enable_full(monkeypatch)
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)

    c = _make_controller(tmp_path)
    reason = await c._run_pre_tool_call_hooks("x402_request", {"amount_usd": 5}, None)
    assert reason is None
    assert _SpyProvider.calls == []  # never queued -- act-and-report lane
