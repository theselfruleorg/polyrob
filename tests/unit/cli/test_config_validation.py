"""`polyrob config set` validated routing + `polyrob config check` (owner-UX P1 T8).

Covers the routing decision tree in the task brief:
  1. secret-shaped KEY  -> env file, unvalidated (today's path).
  2. dotted KEY in PREF_SCHEMA -> per-user preferences.toml (requires --user;
     guarded keys additionally require --confirm).
  3. KEY documented in the flags catalog -> shape-checked env-flag write
     (this now also covers DEFAULT_MODEL/DEFAULT_PROVIDER/CHAT_MODEL/
     CHAT_PROVIDER since their owner-UX P1 T10 catalog rows landed).
  4. otherwise -> hard-reject with `click.ClickException` (suggestion when a
     close match exists) unless --force.

Plus `config check`, which mirrors the `/config check` REPL checker
(``core.prefs.check_env_files`` / ``find_invalid_preferences``).
"""
from pathlib import Path

from click.testing import CliRunner

from cli.commands.config import config


def test_unknown_flag_rejected_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "GOAL_DAILY_QOUTA", "4"])
    assert r.exit_code != 0 and "GOAL_DAILY_QUOTA" in r.output   # suggestion


def test_unknown_flag_written_with_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "MY_CUSTOM_THING", "1", "--force"])
    assert r.exit_code == 0
    assert "MY_CUSTOM_THING=1" in (tmp_path / ".polyrob" / ".env").read_text()


def test_known_flag_and_secret_still_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(config, ["set", "GOAL_DAILY_QUOTA", "4"]).exit_code == 0
    assert CliRunner().invoke(config, ["set", "OPENAI_API_KEY", "sk-x"]).exit_code == 0


def test_pref_key_needs_user(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "style.verbosity", "terse"])
    assert r.exit_code != 0 and "--user" in r.output
    r = CliRunner().invoke(config, ["set", "style.verbosity", "terse",
                                    "--user", "u1", "--home", str(tmp_path)])
    assert r.exit_code == 0


# ---------------------------------------------------------------------------
# secret path — validated separately from the "unknown key" branch
# ---------------------------------------------------------------------------


def test_secret_key_writes_raw_without_force_even_if_unrecognized(tmp_path, monkeypatch):
    # "MY_CUSTOM_API_TOKEN" matches no catalog entry AND no PREF_SCHEMA key, but
    # is_secret_key() flags it (contains "TOKEN") — the secret branch must win
    # BEFORE the unknown-key rejection, with no --force needed.
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "MY_CUSTOM_API_TOKEN", "shh-secret-value"])
    assert r.exit_code == 0, r.output
    text = (tmp_path / ".polyrob" / ".env").read_text()
    assert "MY_CUSTOM_API_TOKEN=shh-secret-value" in text


def test_secret_key_skips_shape_check(tmp_path, monkeypatch):
    # LLM_MAX_OUTPUT_TOKENS is BOTH in the catalog (documented numeric default
    # `16384`) AND secret-shaped (is_secret_key matches "TOKEN" in "TOKENS").
    # The secret branch must be checked FIRST and never shape-validate — if it
    # fell through to the catalog branch instead, this value would be rejected.
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "LLM_MAX_OUTPUT_TOKENS", "not-a-number"])
    assert r.exit_code == 0, r.output
    assert "LLM_MAX_OUTPUT_TOKENS=not-a-number" in (tmp_path / ".polyrob" / ".env").read_text()


# ---------------------------------------------------------------------------
# shape mismatch — a catalog hit with a value that doesn't match the
# documented default's shape is always rejected (no --force override; the
# operator must supply a valid value).
# ---------------------------------------------------------------------------


def test_shape_mismatch_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "GOAL_DAILY_QUOTA", "notanumber"])
    assert r.exit_code != 0
    assert "GOAL_DAILY_QUOTA" in r.output
    assert not (tmp_path / ".polyrob" / ".env").exists() or \
        "GOAL_DAILY_QUOTA=notanumber" not in (tmp_path / ".polyrob" / ".env").read_text()


def test_bool_shape_mismatch_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "CODE_EXEC_ENABLED", "purple"])
    assert r.exit_code != 0
    assert "CODE_EXEC_ENABLED" in r.output


def test_bool_shape_match_accepted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "CODE_EXEC_ENABLED", "true"])
    assert r.exit_code == 0, r.output


# ---------------------------------------------------------------------------
# unknown keys hard-reject unless --force (review fix): ANY unrecognized key
# — close match or not — is refused without --force. Keys with a catalog row
# (DEFAULT_MODEL etc., since owner-UX P1 T10) write unrestricted via the
# catalog-match path (step 3), not a separate allowlist.
# ---------------------------------------------------------------------------


def test_force_writes_unrecognized_key_without_close_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "SOME_BRAND_NEW_PROVIDER_FLAG", "1", "--force"])
    assert r.exit_code == 0, r.output
    assert "SOME_BRAND_NEW_PROVIDER_FLAG=1" in (tmp_path / ".polyrob" / ".env").read_text()


def test_unrecognized_key_without_close_match_rejected_without_force(tmp_path, monkeypatch):
    # MY_CUSTOM_THING has NO difflib close match — it must STILL be rejected
    # without --force (a semantically-different name is not safer than a typo).
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "MY_CUSTOM_THING", "1"])
    assert r.exit_code != 0
    assert "--force" in r.output
    env = tmp_path / ".polyrob" / ".env"
    assert not env.exists() or "MY_CUSTOM_THING" not in env.read_text()


def test_semantic_near_miss_rejected_without_force_written_with_force(tmp_path, monkeypatch):
    # MAX_DAILY_GOALS is a semantic near-miss of GOAL_DAILY_QUOTA; whether or
    # not difflib surfaces a suggestion for it, it must be refused without
    # --force and written with it.
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "MAX_DAILY_GOALS", "4"])
    assert r.exit_code != 0
    r = CliRunner().invoke(config, ["set", "MAX_DAILY_GOALS", "4", "--force"])
    assert r.exit_code == 0, r.output
    assert "MAX_DAILY_GOALS=4" in (tmp_path / ".polyrob" / ".env").read_text()


def test_allowlisted_legacy_key_writes_without_force(tmp_path, monkeypatch):
    # DEFAULT_MODEL is actively read (task_agent_lite/config_store/init) and,
    # since owner-UX P1 T10, has a proper docs/CONFIGURATION.md catalog row —
    # it writes without --force via the catalog-match path (pre-existing
    # test_config_cmd.py contract, now catalog-backed instead of allowlisted).
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "DEFAULT_MODEL", "claude-opus-4-8"])
    assert r.exit_code == 0, r.output
    assert "DEFAULT_MODEL=claude-opus-4-8" in (tmp_path / ".polyrob" / ".env").read_text()


def test_allowlisted_legacy_provider_keys_write_without_force(tmp_path, monkeypatch):
    # DEFAULT_PROVIDER / CHAT_MODEL / CHAT_PROVIDER have difflib close matches
    # (AUX_PROVIDER / HITL_MODE); their catalog rows (owner-UX P1 T10) must be
    # checked BEFORE the closest-match rejection so they still write clean.
    monkeypatch.chdir(tmp_path)
    for key in ("DEFAULT_PROVIDER", "CHAT_MODEL", "CHAT_PROVIDER"):
        r = CliRunner().invoke(config, ["set", key, "x"])
        assert r.exit_code == 0, f"{key}: {r.output}"


# ---------------------------------------------------------------------------
# guarded preference keys require --confirm, same rule as the REPL
# ---------------------------------------------------------------------------


def test_guarded_pref_requires_confirm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "budget.wallet_daily_usd", "5",
                                     "--user", "u1", "--home", str(tmp_path)])
    assert r.exit_code != 0
    assert "--confirm" in r.output

    r = CliRunner().invoke(config, ["set", "budget.wallet_daily_usd", "5",
                                     "--user", "u1", "--home", str(tmp_path), "--confirm"])
    assert r.exit_code == 0, r.output


def test_pref_write_lands_in_preferences_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["set", "style.verbosity", "terse",
                                    "--user", "u1", "--home", str(tmp_path)])
    assert r.exit_code == 0, r.output
    prefs_file = tmp_path / "identity" / "rob" / "user_u1" / "preferences.toml"
    assert prefs_file.exists()
    assert "terse" in prefs_file.read_text()


# ---------------------------------------------------------------------------
# `config check`
# ---------------------------------------------------------------------------


def test_check_clean_reports_no_findings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(config, ["check"])
    assert r.exit_code == 0, r.output
    assert "no findings" in r.output.lower()


def test_check_reports_env_typo_and_never_leaks_secret_value(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".polyrob").mkdir()
    (tmp_path / ".polyrob" / ".env").write_text(
        "GOAL_DAILY_QOUTA=4\nOPENAI_API_KEY=sk-realsecretvalue123\n"
    )
    r = CliRunner().invoke(config, ["check"])
    assert r.exit_code == 0, r.output
    assert "GOAL_DAILY_QOUTA" in r.output
    assert "GOAL_DAILY_QUOTA" in r.output          # suggestion
    assert "sk-realsecretvalue123" not in r.output  # never a secret VALUE


def test_check_reports_invalid_preferences_with_user(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    from core.instance import self_tier_root
    root = self_tier_root(str(home), "u1")
    root.mkdir(parents=True)
    (root / "preferences.toml").write_text('[style]\nnotarealkey = "x"\n')

    r = CliRunner().invoke(config, ["check", "--user", "u1", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert "notarealkey" in r.output
