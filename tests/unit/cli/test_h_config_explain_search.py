"""P2a (proposal 018): /config explain + /config search over the config service."""
from cli.ui.commands.h_config import ConfigCtx, cmd_config


def _ctx(tmp_path):
    return ConfigCtx(user_id="u1", home_dir=str(tmp_path))


def test_explain_pref_renders_provenance_chain(tmp_path, monkeypatch):
    from core.prefs import write_preference
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "10")
    write_preference(tmp_path, "u1", "goals.daily_quota", 4)
    out = cmd_config(_ctx(tmp_path), ["explain", "goals.daily_quota"])
    assert "goals.daily_quota" in out
    assert "pref" in out and "env" in out
    assert "4" in out  # merged effective (min)


def test_explain_flag_shows_sources(tmp_path, monkeypatch):
    monkeypatch.delenv("GOALS_ENABLED", raising=False)
    out = cmd_config(_ctx(tmp_path), ["explain", "GOALS_ENABLED"])
    assert "GOALS_ENABLED" in out
    assert "built-in" in out or "default" in out
    assert "restart" in out


def test_explain_unknown_key_is_graceful(tmp_path):
    out = cmd_config(_ctx(tmp_path), ["explain", "definitely.not_real"])
    assert "unknown key" in out


def test_search_spans_both_namespaces(tmp_path):
    out = cmd_config(_ctx(tmp_path), ["search", "wallet"])
    assert "budget.wallet_daily_usd" in out
    assert "WALLET_DAILY_CAP_USD" in out


def test_search_empty_query_hint(tmp_path):
    out = cmd_config(_ctx(tmp_path), ["search"])
    assert "usage" in out.lower()


def test_completer_offers_config_subcommands_and_keys():
    from cli.ui.commands import SlashCompleter, build_default_registry

    class _Doc:
        def __init__(self, text):
            self.text_before_cursor = text

    comp = SlashCompleter(build_default_registry())
    subs = [c.text for c in comp.get_completions(_Doc("/config "), None)]
    assert "explain" in subs and "set" in subs and "search" in subs

    keys = [c.text for c in comp.get_completions(_Doc("/config explain goals."), None)]
    assert "goals.daily_quota" in keys

    flags = [c.text for c in comp.get_completions(_Doc("/config set GOALS_EN"), None)]
    assert "GOALS_ENABLED" in flags
