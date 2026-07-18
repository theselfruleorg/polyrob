"""P2b (proposal 018): /config interactive picker — adapter over the frozen widget.

The renderer freeze allows NO new rendering abstractions, so the settings
picker REUSES the existing /model ReplPicker verbatim: settings are presented
as ModelChoice rows (provider=group header, model=key, pricing_hint=value
summary + badges). Selection resolves to (group, key); the handler prefills
the input buffer with a ready-to-send `/config set KEY …` (bools arrive
pre-toggled so Enter applies immediately).
"""
from cli.ui.config_picker import build_setting_choices, prefill_for


def test_choices_cover_both_namespaces_with_badges(tmp_path):
    choices = build_setting_choices("u1", str(tmp_path))
    by_key = {c.model: c for c in choices}
    assert "goals.daily_quota" in by_key
    assert "GOALS_ENABLED" in by_key
    row = by_key["goals.daily_quota"]
    assert row.provider == "goals"            # group header
    assert row.display_name == "goals.daily_quota"
    assert "6" in row.pricing_hint            # honest built-in default
    guarded = by_key["budget.wallet_daily_usd"]
    assert "⛨" in guarded.pricing_hint        # guarded badge
    advisory = by_key["style.tone"]
    assert "≈" in advisory.pricing_hint       # advisory badge
    flag = by_key["GOALS_ENABLED"]
    assert "↻" in flag.pricing_hint           # restart badge


def test_prefill_toggles_bools_and_seeds_others(tmp_path):
    from core import config_service
    info = config_service.describe("GOALS_ENABLED", user_id="u1",
                                   home_dir=str(tmp_path))
    text = prefill_for(info)
    assert text.startswith("/config set GOALS_ENABLED ")
    assert text.rstrip().endswith(("on", "off"))  # pre-toggled

    quota = config_service.describe("goals.daily_quota", user_id="u1",
                                    home_dir=str(tmp_path))
    assert prefill_for(quota) == "/config set goals.daily_quota "

    guarded = config_service.describe("budget.wallet_daily_usd", user_id="u1",
                                      home_dir=str(tmp_path))
    assert prefill_for(guarded).startswith("/config set budget.wallet_daily_usd ")
    assert "--confirm" in prefill_for(guarded)
