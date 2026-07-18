"""Resolver precedence + tighten-only merge semantics (spec §3.2, §3.4)."""
import pytest

from core.prefs import resolve, resolve_with_source, write_preference


@pytest.fixture()
def home(tmp_path):
    return tmp_path


def test_default_when_no_pref_no_env(home):
    val, src = resolve_with_source("goals.notify_on_done", "u1", home,
                                   env_value=None, default=True)
    assert val is True and src == "default"


def test_env_beats_default(home):
    val, src = resolve_with_source("goals.notify_on_done", "u1", home,
                                   env_value=False, default=True)
    assert val is False and src == "env"


def test_override_pref_beats_env(home):
    write_preference(home, "u1", "digest.channel", "email")
    val, src = resolve_with_source("digest.channel", "u1", home,
                                   env_value="telegram", default="telegram")
    assert val == "email" and src == "pref"


def test_min_merge_budget_cannot_widen(home):
    write_preference(home, "u1", "budget.wallet_daily_usd", 50.0)
    val, src = resolve_with_source("budget.wallet_daily_usd", "u1", home,
                                   env_value=10.0, default=None)
    assert val == 10.0 and src == "merged(min)"      # env cap is the floor
    write_preference(home, "u1", "budget.wallet_daily_usd", 5.0)
    val, _ = resolve_with_source("budget.wallet_daily_usd", "u1", home,
                                 env_value=10.0, default=None)
    assert val == 5.0                                 # tighter pref wins


def test_union_merge_approvals(home):
    write_preference(home, "u1", "approvals.require", ["x402_request"])
    val, src = resolve_with_source("approvals.require", "u1", home,
                                   env_value=["git_push"], default=[])
    assert val == ["git_push", "x402_request"] and src == "merged(union)"


def test_narrow_list_merge_cannot_widen_past_env(home):
    """Allowlist polarity (T5 review fix): unlike union, a pref entry the
    operator never listed must NOT widen the reachable set."""
    write_preference(home, "u1", "outbound.domains", ["attacker.com"])
    val, src = resolve_with_source("outbound.domains", "u1", home,
                                   env_value=["corp.io"], default=[])
    assert val == [] and src == "merged(narrow)"


def test_narrow_list_merge_intersects(home):
    write_preference(home, "u1", "outbound.domains", ["corp.io", "extra.com"])
    val, src = resolve_with_source("outbound.domains", "u1", home,
                                   env_value=["corp.io"], default=[])
    assert val == ["corp.io"] and src == "merged(narrow)"


def test_narrow_list_merge_defines_set_when_env_empty(home):
    """With no operator restriction at all, there's no ceiling to widen past —
    the guarded+approvable pref channel defines the set from scratch."""
    write_preference(home, "u1", "outbound.domains", ["corp.io"])
    val, src = resolve_with_source("outbound.domains", "u1", home,
                                   env_value=[], default=[])
    assert val == ["corp.io"] and src == "pref"


def test_and_merge_capability_only_disables(home):
    write_preference(home, "u1", "autonomy.self_wake", True)
    assert resolve("autonomy.self_wake", "u1", home,
                   env_value=False, default=False) is False   # env OFF stays OFF
    write_preference(home, "u1", "autonomy.self_wake", False)
    assert resolve("autonomy.self_wake", "u1", home,
                   env_value=True, default=False) is False    # pref can disable


def test_stricter_provider(home):
    write_preference(home, "u1", "approvals.provider", "auto")
    assert resolve("approvals.provider", "u1", home,
                   env_value="interactive_cli", default="auto") == "interactive_cli"
    write_preference(home, "u1", "approvals.provider", "deny")
    assert resolve("approvals.provider", "u1", home,
                   env_value="interactive_cli", default="auto") == "deny"


def test_prefs_enabled_kill_switch(home, monkeypatch):
    write_preference(home, "u1", "digest.channel", "email")
    monkeypatch.setenv("PREFS_ENABLED", "false")
    val, src = resolve_with_source("digest.channel", "u1", home,
                                   env_value=None, default="telegram")
    assert val == "telegram" and src == "default"


def test_stricter_provider_never_overrides_custom_env_provider(home):
    """Custom (non-standard) APPROVAL_PROVIDER must never be overridden by a pref."""
    write_preference(home, "u1", "approvals.provider", "auto")
    val, src = resolve_with_source("approvals.provider", "u1", home,
                                   env_value="my_custom_provider", default="auto")
    assert val == "my_custom_provider" and src == "env"


def test_resolve_without_instance_id_honors_custom_instance(home, monkeypatch):
    """owner-UX P2-4 final review, item 1: enforcement call sites like
    ``prefs.resolve("budget.wallet_daily_usd", user_id, home_dir, ...)``
    (core/wallet/config.py, agents/task/goals/dispatcher.py, cron/digest.py,
    cron/delivery.py, tools/controller/approval.py, tools/controller/service.py)
    never thread an explicit instance_id through — they must still see a pref
    written under a non-default POLYROB_INSTANCE_ID, not silently fall back to
    the "rob" literal default and ignore it."""
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "custom")
    write_preference(home, "u1", "budget.wallet_daily_usd", 5.0)
    val, src = resolve_with_source("budget.wallet_daily_usd", "u1", home,
                                   env_value=10.0, default=None)
    assert val == 5.0 and src == "merged(min)"
