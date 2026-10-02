"""Tests for the tiered defi spend lane (proposal 023 §5.3 D3).

Owner decision 2026-08-19: a within-ceiling on-chain trade executes without a
pre-approval tap; above the ceiling it still queues. The dry-run exemption is
unconditional — `_run_guarded` returns before `sign_and_send`, so a simulation
can never move money and blocking it only cost the agent its ability to quote.
"""
import pytest

from core.config_policy.spend_lane import defi_spend_exemption


def _live(**kw):
    p = {"chain": "base", "dry_run": False, "max_spend_usd": 0.5}
    p.update(kw)
    return p


class TestDryRunAlwaysExempt:
    """A simulation broadcasts nothing, in any mode, flag on or off."""

    @pytest.mark.parametrize("verb", [
        "defi_trade_swap", "defi_trade_transfer",
        "defi_trade_approve_token", "defi_trade_revoke_approval",
    ])
    def test_explicit_dry_run_is_exempt(self, verb, monkeypatch):
        monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
        reason = defi_spend_exemption(verb, {"dry_run": True, "max_spend_usd": 999.0})
        assert reason and "dry" in reason.lower()

    def test_absent_dry_run_is_treated_as_dry_run(self, monkeypatch):
        """Every param model defaults dry_run=True, so absent means simulate."""
        monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
        assert defi_spend_exemption("defi_trade_swap", {"max_spend_usd": 999.0})


class TestFlagOffKeepsTheOwnerQueue:
    def test_live_spend_is_not_exempt_when_flag_off(self, monkeypatch):
        monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
        assert defi_spend_exemption("defi_trade_swap", _live()) is None

    def test_live_spend_is_not_exempt_when_flag_explicitly_off(self, monkeypatch):
        monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "false")
        assert defi_spend_exemption("defi_trade_swap", _live()) is None


class TestFlagOnTiersByDeclaredCeiling:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true")
        monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "1")

    def test_within_ceiling_is_exempt(self):
        assert defi_spend_exemption("defi_trade_swap", _live(max_spend_usd=0.9))

    def test_at_the_ceiling_is_exempt(self):
        assert defi_spend_exemption("defi_trade_swap", _live(max_spend_usd=1.0))

    def test_above_the_ceiling_still_queues(self):
        assert defi_spend_exemption("defi_trade_swap", _live(max_spend_usd=1.01)) is None

    def test_missing_declaration_still_queues(self):
        p = _live(); p.pop("max_spend_usd")
        assert defi_spend_exemption("defi_trade_swap", p) is None

    def test_non_numeric_declaration_still_queues(self):
        assert defi_spend_exemption("defi_trade_swap", _live(max_spend_usd="cheap")) is None

    def test_revoke_is_exempt_without_a_declaration(self):
        """revoke_approval sets an allowance to ZERO — it can only reduce risk."""
        assert defi_spend_exemption(
            "defi_trade_revoke_approval", {"dry_run": False}) is not None

    def test_a_non_defi_money_verb_is_never_exempt(self):
        for verb in ("x402_pay_pay", "hyperliquid_place_market_order",
                     "polymarket_place_limit_order"):
            assert defi_spend_exemption(verb, _live()) is None


_SPEND_TOOL_CLASSES = (
    ("defi_trade", "tools.defi.trade_tool", "DefiTradeTool"),
    ("launchpad", "tools.launchpad.tool", "LaunchpadTool"),
    ("dapp_browser", "tools.dapp_browser.tool", "DappBrowserTool"),
    ("agent_nft", "tools.agent_nft.tool", "AgentNftTool"),
)


def _param_model_for(verb):
    """Resolve a RUNTIME spend verb to its param model, across every tool.

    FAILS (never skips) when a verb cannot be resolved: an unresolvable verb is
    one whose simulation claim nobody checked.
    """
    import importlib
    import inspect
    for prefix, module, cls_name in _SPEND_TOOL_CLASSES:
        if not verb.startswith(prefix + "_"):
            continue
        cls = getattr(importlib.import_module(module), cls_name)
        for attr in (verb[len(prefix) + 1:], verb):
            try:
                member = inspect.getattr_static(cls, attr)
            except AttributeError:
                continue
            model = getattr(member, "_param_model", None)
            assert model is not None, f"{verb}: action has no param model"
            return model
        pytest.fail(f"{verb}: no action '{verb}' on {cls_name}")
    pytest.fail(f"{verb}: no tool class known for this verb — add it to "
                f"_SPEND_TOOL_CLASSES")


class TestParamModelDefaultsArePinned:
    """The absent-dry_run exemption is only sound while the models default True."""

    @pytest.mark.parametrize("verb", sorted(
        __import__("core.config_policy.spend_lane",
                   fromlist=["DEFI_SPEND_VERBS"]).DEFI_SPEND_VERBS))
    def test_every_spend_verb_agrees_with_dry_run_verbs(self, verb):
        """H03a (2026-09-23): EVERY verb in DEFI_SPEND_VERBS, across ALL tools.

        The old derivation reflected over DefiTradeTool only and SKIPPED a model
        without ``dry_run`` — which is exactly how `dapp_browser_dapp_connect`
        (no dry_run field) rode the "absent means simulate" default past the
        owner queue. A verb is in DRY_RUN_VERBS iff its model has a dry_run
        field, and that field must default True.
        """
        from core.config_policy.spend_lane import DRY_RUN_VERBS
        model = _param_model_for(verb)
        field = getattr(model, "model_fields", {}).get("dry_run")
        if field is None:
            assert verb not in DRY_RUN_VERBS, (
                f"{verb}: {model.__name__} has NO dry_run field, so it can never "
                f"be a simulation — remove it from DRY_RUN_VERBS")
        else:
            assert verb in DRY_RUN_VERBS, (
                f"{verb}: {model.__name__} has a dry_run field; add it to "
                f"DRY_RUN_VERBS so a dry run is not queued")
            assert field.default is True, f"{verb}: dry_run must default True"


class TestVerbWithoutDryRunIsNeverASimulation:
    def test_dapp_connect_absent_dry_run_is_gated(self, monkeypatch):
        monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
        assert defi_spend_exemption("dapp_browser_dapp_connect",
                                    {"url": "https://app.example"}) is None

    def test_dapp_connect_with_a_smuggled_dry_run_is_gated(self, monkeypatch):
        monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
        assert defi_spend_exemption("dapp_browser_dapp_connect",
                                    {"dry_run": True}) is None

    def test_is_simulation_needs_a_literal_true(self):
        from core.config_policy.spend_lane import is_simulation
        assert is_simulation("defi_trade_swap", {}) is True
        assert is_simulation("defi_trade_swap", {"dry_run": True}) is True
        assert is_simulation("defi_trade_swap", {"dry_run": False}) is False
        assert is_simulation("defi_trade_swap", {"dry_run": "false"}) is False
        assert is_simulation("unknown_verb", {"dry_run": True}) is False


def test_x402_fetch_below_ceiling_is_exempt(monkeypatch):
    """A micro-payment inside the x402 autonomous ceiling runs act-and-report;
    blocking every $0.001 fetch on an owner tap would make x402 unusable."""
    monkeypatch.setenv("X402_AUTONOMOUS_MAX_USD", "1.0")
    from core.config_policy.spend_lane import spend_exemption
    reason = spend_exemption("x402_pay_x402_fetch", {"max_amount_usd": 0.05})
    assert reason and "0.05" in reason


def test_x402_fetch_above_ceiling_keeps_the_owner_tap(monkeypatch):
    monkeypatch.setenv("X402_AUTONOMOUS_MAX_USD", "1.0")
    from core.config_policy.spend_lane import spend_exemption
    assert spend_exemption("x402_pay_x402_fetch", {"max_amount_usd": 5.0}) is None


def test_x402_fetch_with_no_declared_amount_keeps_the_tap():
    from core.config_policy.spend_lane import spend_exemption
    assert spend_exemption("x402_pay_x402_fetch", {}) is None
    assert spend_exemption("x402_pay_x402_fetch", {"max_amount_usd": "cheap"}) is None
    assert spend_exemption("x402_pay_x402_fetch", {"max_amount_usd": True}) is None
    assert spend_exemption("x402_pay_x402_fetch", {"max_amount_usd": -1}) is None


def test_x402_exemption_needs_no_tiered_lane_flag(monkeypatch):
    """DEFI_TIERED_SPEND_LANE gates the on-chain lane only. x402 has its own,
    much smaller ceiling and is off-by-cap, not off-by-flag — otherwise the
    default posture is 'every micro-payment blocks', which is not shippable."""
    monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
    monkeypatch.setenv("X402_AUTONOMOUS_MAX_USD", "1.0")
    from core.config_policy.spend_lane import spend_exemption
    assert spend_exemption("x402_pay_x402_fetch", {"max_amount_usd": 0.5}) is not None


def test_defi_verbs_are_unchanged_by_the_rename(monkeypatch):
    """The DeFi behaviour must be byte-identical — only the function name widened."""
    from core.config_policy.spend_lane import spend_exemption, defi_spend_exemption
    assert defi_spend_exemption is spend_exemption  # the alias must BE the function
    assert defi_spend_exemption("defi_trade_swap", {"dry_run": True})
    monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
    assert defi_spend_exemption(
        "defi_trade_swap", {"dry_run": False, "max_spend_usd": 1.0}) is None


def test_x402_fetch_is_on_the_spend_lane_not_the_receive_lane():
    from core.config_policy import (
        PAYMENT_APPROVAL_TOOLS, PAYMENT_RECEIVE_APPROVAL_TOOLS)
    assert "x402_pay_x402_fetch" in PAYMENT_APPROVAL_TOOLS
    assert "x402_pay_x402_fetch" not in PAYMENT_RECEIVE_APPROVAL_TOOLS
