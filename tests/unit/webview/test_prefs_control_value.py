"""2026-11-20 live review: Settings › Save wrote settings the owner never
touched, because the read sent a default's display words as the control's
value. The read now sends `control_value` beside the display `value`."""
from webview.prefs_view import control_value, source_words


def test_a_defaults_display_words_are_never_a_control_value():
    assert control_value("launch.max_pool_usd", "not set (the per-transaction limit binds)",
                         "built-in") is None
    assert control_value("launch.pair", "not set (any pair)", "built-in") is None
    assert control_value("owner.timezone", "server clock", "built-in") is None
    assert control_value("approvals.provider", "`auto`", "built-in") is None


def test_a_valid_default_and_a_set_value_are_the_control_value():
    assert control_value("budget.wallet_daily_usd", 100, "built-in") == 100.0
    assert control_value("ui.theme", "auto", "built-in") == "auto"
    assert control_value("autonomy.self_wake", False, "default(x)") is False
    assert control_value("launch.pair", "WETH", "pref") == "WETH"
    assert control_value("launch.pair", "WETH", "env") == "WETH"


def test_source_words_drop_the_resolver_wrapper():
    assert source_words("default(default (no bundle))") == "default (no bundle)"
    assert source_words("built-in") == "built-in"
