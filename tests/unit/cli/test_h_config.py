"""Tests for the ``/config`` REPL slash-command handler
(cli/ui/commands/h_config.py) — owner-UX P1 T7.

``cmd_config(ctx, args) -> str`` is driven directly as a function (the
established pattern for ``h_*`` modules) via a tiny ``ConfigCtx`` dataclass so
the handler is testable without a live REPL. The brief's four tests (Step 1)
are verbatim; the rest extend coverage for ``list`` output shape, ``get`` on
an env-flag/catalog key, and ``set`` against the flags catalog.
"""
import pytest

from core.prefs import load_preferences
import tools.controller.approval as approval


@pytest.fixture(autouse=True)
def _clean_approval_env(monkeypatch):
    """Isolate the module-level frozen-approval-provider snapshot (approval.py
    freezes ``APPROVAL_PROVIDER`` at import for WS-7 mutation-proofing) so the
    item-3 enforcement-warning tests don't leak state into/out of other
    ``/config`` tests."""
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    approval._refreeze_approval_flags_for_tests()
    yield
    approval._refreeze_approval_flags_for_tests()


def _ctx(tmp_path):
    from cli.ui.commands.h_config import ConfigCtx
    return ConfigCtx(user_id="u1", home_dir=tmp_path)


# ---------------------------------------------------------------------------
# Brief's Step 1 tests (verbatim contract)
# ---------------------------------------------------------------------------


def test_set_and_get_pref(tmp_path):
    from cli.ui.commands.h_config import cmd_config
    out = cmd_config(_ctx(tmp_path), ["set", "style.verbosity", "terse"])
    assert "terse" in out and "next-turn" in out            # applies shown
    assert load_preferences(tmp_path, "u1")["style.verbosity"] == "terse"
    out = cmd_config(_ctx(tmp_path), ["get", "style.verbosity"])
    assert "pref" in out


def test_set_unknown_key_suggests(tmp_path):
    from cli.ui.commands.h_config import cmd_config
    out = cmd_config(_ctx(tmp_path), ["set", "style.verbose", "terse"])
    assert "unknown" in out.lower() and "style.verbosity" in out


def test_guarded_key_requires_confirm(tmp_path):
    from cli.ui.commands.h_config import cmd_config
    out = cmd_config(_ctx(tmp_path), ["set", "budget.wallet_daily_usd", "5"])
    assert "guarded" in out.lower()
    assert "budget.wallet_daily_usd" not in load_preferences(tmp_path, "u1")
    cmd_config(_ctx(tmp_path), ["set", "budget.wallet_daily_usd", "5", "--confirm"])
    assert load_preferences(tmp_path, "u1")["budget.wallet_daily_usd"] == 5.0


def test_check_reports_unknown_env_key(tmp_path, monkeypatch):
    from cli.ui.commands.h_config import cmd_config
    envfile = tmp_path / ".polyrob" / ".env"
    envfile.parent.mkdir(parents=True)
    envfile.write_text("GOAL_DAILY_QOUTA=4\n")               # typo on purpose
    monkeypatch.chdir(tmp_path)
    out = cmd_config(_ctx(tmp_path), ["check"])
    assert "GOAL_DAILY_QOUTA" in out and "GOAL_DAILY_QUOTA" in out


# ---------------------------------------------------------------------------
# list — grouped output shape
# ---------------------------------------------------------------------------


def test_list_groups_keys_and_shows_source_and_applies(tmp_path):
    from cli.ui.commands.h_config import cmd_config
    out = cmd_config(_ctx(tmp_path), ["list"])
    assert "── style" in out
    assert "── budget" in out
    # every row: "key  value   (source, applies: <applies>)" (candy.kv_lines)
    assert "style.verbosity" in out
    assert "applies: next-turn" in out
    assert "(default" in out or "(pref" in out


def test_list_reflects_a_written_pref_with_pref_source(tmp_path):
    from cli.ui.commands.h_config import cmd_config
    cmd_config(_ctx(tmp_path), ["set", "style.verbosity", "detailed"])
    out = cmd_config(_ctx(tmp_path), ["list", "style"])
    assert "style.verbosity" in out and "detailed" in out
    # 018 P0.4: style.* rows carry the advisory label (prompt-only enforcement).
    assert "(pref, applies: next-turn, advisory)" in out
    # group filter excludes other groups
    assert "── budget" not in out


def test_list_unknown_group_is_graceful(tmp_path):
    from cli.ui.commands.h_config import cmd_config
    out = cmd_config(_ctx(tmp_path), ["list", "nonexistent-group"])
    assert "no preferences" in out.lower()


# ---------------------------------------------------------------------------
# get — pref keys (source/applies/description/sensitivity) + catalog keys
# ---------------------------------------------------------------------------


def test_get_shows_description_and_applies(tmp_path):
    from cli.ui.commands.h_config import cmd_config
    out = cmd_config(_ctx(tmp_path), ["get", "goals.daily_quota"])
    assert "applies: live" in out
    assert "Autonomous goal runs per day" in out


def test_get_guarded_pref_notes_sensitivity(tmp_path):
    from cli.ui.commands.h_config import cmd_config
    out = cmd_config(_ctx(tmp_path), ["get", "budget.wallet_daily_usd"])
    assert "guarded" in out.lower()


def test_get_unknown_key_suggests_closest(tmp_path):
    from cli.ui.commands.h_config import cmd_config
    # "goal.daily_quota" (missing the 's') is neither a substring of nor a
    # superstring of the real key "goals.daily_quota" — a clean suggestion test.
    out = cmd_config(_ctx(tmp_path), ["get", "goal.daily_quota"])
    assert "unknown" in out.lower() and "goals.daily_quota" in out


def test_get_catalog_env_flag_key(tmp_path):
    from cli.ui.commands.h_config import cmd_config
    out = cmd_config(_ctx(tmp_path), ["get", "GOAL_DAILY_QUOTA"])
    assert "GOAL_DAILY_QUOTA" in out
    assert "env-flag" in out.lower()


# ---------------------------------------------------------------------------
# set — env-flag (catalog) keys: project-scope write, shape validation
# ---------------------------------------------------------------------------


def test_set_catalog_flag_writes_project_env(tmp_path, monkeypatch):
    from cli.ui.commands.h_config import cmd_config
    monkeypatch.chdir(tmp_path)
    out = cmd_config(_ctx(tmp_path), ["set", "CODE_EXEC_ENABLED", "true"])
    assert "restart" in out.lower()
    written = (tmp_path / ".polyrob" / ".env").read_text()
    assert "CODE_EXEC_ENABLED=true" in written


def test_set_catalog_flag_rejects_shape_mismatch(tmp_path, monkeypatch):
    from cli.ui.commands.h_config import cmd_config
    monkeypatch.chdir(tmp_path)
    # CODE_EXEC_MAX_TIMEOUT_SEC documents a numeric default (`30`)
    out = cmd_config(_ctx(tmp_path), ["set", "CODE_EXEC_MAX_TIMEOUT_SEC", "soon"])
    assert "error" in out.lower()
    envfile = tmp_path / ".polyrob" / ".env"
    assert not envfile.exists() or "CODE_EXEC_MAX_TIMEOUT_SEC" not in envfile.read_text()


def test_set_unknown_key_suggests_across_catalog_namespace(tmp_path, monkeypatch):
    from cli.ui.commands.h_config import cmd_config
    monkeypatch.chdir(tmp_path)
    out = cmd_config(_ctx(tmp_path), ["set", "CODE_EXEC_ENABLE", "true"])
    assert "unknown" in out.lower() and "CODE_EXEC_ENABLED" in out


# ---------------------------------------------------------------------------
# set approvals.require — owner-UX P1 final review (item 3): warn when the
# freshly-registered gate has no enforcing provider behind it.
# ---------------------------------------------------------------------------


def test_approvals_require_warns_when_provider_is_auto(tmp_path, monkeypatch):
    from cli.ui.commands.h_config import cmd_config
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    approval._refreeze_approval_flags_for_tests()
    out = cmd_config(_ctx(tmp_path), ["set", "approvals.require", "git_push", "--confirm"])
    assert "git_push" in out
    assert "auto" in out.lower() and "interactive_cli" in out.lower()
    assert "⚠" in out


def test_approvals_require_no_warning_when_provider_is_interactive_cli(tmp_path, monkeypatch):
    from cli.ui.commands.h_config import cmd_config
    monkeypatch.setenv("APPROVAL_PROVIDER", "interactive_cli")
    approval._refreeze_approval_flags_for_tests()
    out = cmd_config(_ctx(tmp_path), ["set", "approvals.require", "git_push", "--confirm"])
    assert "git_push" in out
    assert "⚠" not in out


def test_approvals_require_no_warning_when_pref_sets_interactive_provider(tmp_path, monkeypatch):
    """The provider can also come from an ``approvals.provider`` pref (stricter
    merge) rather than the env — the warning must consult the FULLY resolved
    effective provider, not just the raw env snapshot."""
    from cli.ui.commands.h_config import cmd_config
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    approval._refreeze_approval_flags_for_tests()
    cmd_config(_ctx(tmp_path), ["set", "approvals.provider", "interactive_cli", "--confirm"])
    out = cmd_config(_ctx(tmp_path), ["set", "approvals.require", "git_push", "--confirm"])
    assert "⚠" not in out


def test_other_pref_sets_never_show_approval_warning(tmp_path):
    from cli.ui.commands.h_config import cmd_config
    out = cmd_config(_ctx(tmp_path), ["set", "style.verbosity", "terse"])
    assert "⚠" not in out


# ---------------------------------------------------------------------------
# _display_value_source — owner-UX P1 final review (item 4): show the real
# EFFECTIVE value/source, not a partial view that hides the merge.
# ---------------------------------------------------------------------------


def test_get_shows_merged_value_not_raw_env_when_pref_also_set(tmp_path, monkeypatch):
    """env cap 10 + pref 5 must show the merged 5.0, not the raw env '10'."""
    from cli.ui.commands.h_config import cmd_config
    monkeypatch.setenv("WALLET_DAILY_CAP_USD", "10")
    cmd_config(_ctx(tmp_path), ["set", "budget.wallet_daily_usd", "5", "--confirm"])
    out = cmd_config(_ctx(tmp_path), ["get", "budget.wallet_daily_usd"])
    assert "5.0" in out
    assert "merged(min)" in out
    assert "= 10" not in out


def test_list_shows_merged_value_for_wallet_cap(tmp_path, monkeypatch):
    from cli.ui.commands.h_config import cmd_config
    monkeypatch.setenv("WALLET_DAILY_CAP_USD", "10")
    cmd_config(_ctx(tmp_path), ["set", "budget.wallet_daily_usd", "5", "--confirm"])
    out = cmd_config(_ctx(tmp_path), ["list", "budget"])
    assert "budget.wallet_daily_usd" in out and "5.0" in out
    assert "merged(min)" in out


def test_get_and_merge_pref_shown_as_pref_when_env_unset(tmp_path, monkeypatch):
    """A just-set autonomy.self_wake=true pref must show True/"pref" — not
    False/"merged(and)" — when the env flag is unset."""
    from cli.ui.commands.h_config import cmd_config
    monkeypatch.delenv("SELF_WAKE_ENABLED", raising=False)
    cmd_config(_ctx(tmp_path), ["set", "autonomy.self_wake", "true"])
    out = cmd_config(_ctx(tmp_path), ["get", "autonomy.self_wake"])
    assert "True" in out
    assert "(pref)" in out
    assert "False" not in out


def test_get_env_only_key_still_shows_env(tmp_path, monkeypatch):
    from cli.ui.commands.h_config import cmd_config
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "9")
    out = cmd_config(_ctx(tmp_path), ["get", "goals.daily_quota"])
    assert "9" in out and "(env)" in out


# ---------------------------------------------------------------------------
# check — env-file paths come from the R-1 SSOT helper
# ---------------------------------------------------------------------------


def test_check_paths_come_from_env_file_candidates(tmp_path, monkeypatch):
    """/config check must feed check_env_files the helper-derived order
    (later-wins merge => reversed precedence: home THEN project)."""
    from pathlib import Path
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; (proj / ".polyrob").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("POLYROB_HOME", raising=False)
    monkeypatch.chdir(proj)
    captured = {}

    def fake_check(paths):
        captured["paths"] = [Path(p) for p in paths]
        return []
    monkeypatch.setattr("core.prefs.check_env_files", fake_check)
    from cli.ui.commands.h_config import _cmd_check
    _cmd_check(_ctx(tmp_path), [])
    assert captured["paths"] == [home / ".polyrob" / ".env",
                                 proj / ".polyrob" / ".env"]
