from pathlib import Path
from click.testing import CliRunner


def test_config_set_global_and_show(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    from cli.commands.config import config as config_group
    r = CliRunner()
    res = r.invoke(config_group, ["set", "ANTHROPIC_API_KEY", "sk-secret", "--global"])
    assert res.exit_code == 0, res.output
    assert "ANTHROPIC_API_KEY=sk-secret" in (home / ".polyrob" / ".env").read_text()
    assert (home / ".polyrob" / ".env").stat().st_mode & 0o777 == 0o600
    res = r.invoke(config_group, ["set", "DEFAULT_MODEL", "claude-opus-4-8"])
    assert res.exit_code == 0, res.output
    assert "DEFAULT_MODEL=claude-opus-4-8" in (proj / ".polyrob" / ".env").read_text()
    res = r.invoke(config_group, ["show"])
    assert res.exit_code == 0, res.output
    assert "sk-secret" not in res.output            # secret redacted
    assert "ANTHROPIC_API_KEY" in res.output        # key name shown
    assert "claude-opus-4-8" in res.output          # non-secret shown


def test_config_set_project_gitignores_polyrob(tmp_path, monkeypatch):
    # A project-scope `config set` writes a secret to ./.polyrob/.env; it must add
    # .polyrob/ to .gitignore so a subsequent `git add` can't leak it (the gap:
    # only init/run gitignored it before, neither of which has run yet).
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()  # a git repo → require_git_repo=True should write
    monkeypatch.chdir(proj)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["set", "OPENAI_API_KEY", "sk-realkey123456", "--project"])
    assert res.exit_code == 0, res.output
    gi = proj / ".gitignore"
    assert gi.exists(), "config set (project scope) must create/append .gitignore"
    assert any(ln.strip() == ".polyrob/" for ln in gi.read_text().splitlines())


def test_config_set_no_duplicate_on_spaced_key(tmp_path, monkeypatch):
    # A hand-edited line `KEY = old` (spaces around `=`) was indexed under
    # "KEY " (trailing space), so `config set KEY new` failed to find it and
    # appended a duplicate `KEY=new`. It must instead update the existing line.
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    env = home / ".polyrob" / ".env"
    env.write_text("DEFAULT_MODEL = old\n")
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["set", "DEFAULT_MODEL", "new", "--global"])
    assert res.exit_code == 0, res.output
    lines = [ln for ln in env.read_text().splitlines() if ln.strip()]
    key_lines = [ln for ln in lines if ln.split("=", 1)[0].strip() == "DEFAULT_MODEL"]
    assert key_lines == ["DEFAULT_MODEL=new"], f"expected one updated line, got {lines}"


def test_config_show_redacts_jwt(tmp_path, monkeypatch):
    # ANYSITE_JWT is a real credential (bootstrap secret allowlist) but the old
    # _SECRET_HINTS missed 'JWT' so `config show` printed it in cleartext.
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    (home / ".polyrob" / ".env").write_text("ANYSITE_JWT=eyJhbGcabcdefghij123\n")
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["show"])
    assert res.exit_code == 0, res.output
    assert "eyJhbGcabcdefghij123" not in res.output  # value redacted
    assert "ANYSITE_JWT" in res.output               # key name still shown


def test_config_path_lists_files(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; (proj / ".polyrob").mkdir(parents=True)
    (home / ".polyrob" / ".env").write_text("A=1\n")
    (proj / ".polyrob" / ".env").write_text("B=2\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["path"])
    assert res.exit_code == 0, res.output
    assert str(home / ".polyrob" / ".env") in res.output
    assert str(proj / ".polyrob" / ".env") in res.output


# --- 024 T2: VALUE is optional, prompted, and never in shell history ----------

def _isolated(tmp_path, monkeypatch):
    """Point HOME + CWD at tmp so writes never touch the developer's config."""
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    return home, proj


def test_config_set_prompts_when_value_omitted(tmp_path, monkeypatch):
    """`polyrob config set OPENAI_API_KEY sk-...` puts a live credential in
    shell history and in `ps` output. Omitting VALUE must read it instead."""
    home, _ = _isolated(tmp_path, monkeypatch)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(
        config_group, ["set", "OPENAI_API_KEY", "--global"], input="sk-piped-secret\n"
    )
    assert res.exit_code == 0, res.output
    assert "OPENAI_API_KEY=sk-piped-secret" in (home / ".polyrob" / ".env").read_text()
    # the value must never be echoed back to the terminal
    assert "sk-piped-secret" not in res.output


def test_config_set_prompt_strips_pasted_whitespace(tmp_path, monkeypatch):
    """A key pasted from a browser/manager commonly carries a trailing newline
    or space; storing it verbatim yields a 401 that looks like a bad key."""
    home, _ = _isolated(tmp_path, monkeypatch)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(
        config_group, ["set", "ANTHROPIC_API_KEY", "--global"], input="  sk-padded  \n"
    )
    assert res.exit_code == 0, res.output
    assert "ANTHROPIC_API_KEY=sk-padded" in (home / ".polyrob" / ".env").read_text()


def test_config_set_refuses_blank_value(tmp_path, monkeypatch):
    """A blank credential reads as 'configured' at every presence-only gate."""
    home, _ = _isolated(tmp_path, monkeypatch)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(
        config_group, ["set", "OPENAI_API_KEY", "--global"], input="\n"
    )
    assert res.exit_code != 0
    assert "nothing written" in res.output
    assert not (home / ".polyrob" / ".env").exists() or \
        "OPENAI_API_KEY" not in (home / ".polyrob" / ".env").read_text()


def test_config_set_prompted_value_still_routes_and_validates(tmp_path, monkeypatch):
    """The prompt only supplies VALUE — it must not bypass the routing tree."""
    _isolated(tmp_path, monkeypatch)
    from cli.commands.config import config as config_group
    r = CliRunner()
    # a catalog flag is still shape-checked
    res = r.invoke(config_group, ["set", "SUB_AGENTS_ENABLED"], input="banana\n")
    assert res.exit_code != 0
    assert "expects a" in res.output
    # an unknown key is still rejected without --force
    res = r.invoke(config_group, ["set", "NOT_A_REAL_FLAG_XYZ"], input="1\n")
    assert res.exit_code != 0
    assert "unknown key" in res.output


def test_config_set_with_explicit_value_is_unchanged(tmp_path, monkeypatch):
    """The two-argument form must keep working exactly as before."""
    home, _ = _isolated(tmp_path, monkeypatch)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(
        config_group, ["set", "OPENAI_API_KEY", "sk-explicit", "--global"]
    )
    assert res.exit_code == 0, res.output
    assert "OPENAI_API_KEY=sk-explicit" in (home / ".polyrob" / ".env").read_text()


def test_config_set_hides_input_for_a_secret_on_a_tty(monkeypatch):
    """On a real terminal the credential must not be echoed as it is typed.

    (The CliRunner cases above cover the piped branch, where stdin is not a
    tty and getpass cannot read; this pins the interactive branch.)"""
    import click as _click
    import cli.commands.config as cfg

    seen = {}

    def fake_prompt(text, **kwargs):
        seen[text] = kwargs
        return "sk-typed"

    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr(_click, "prompt", fake_prompt)

    assert cfg._prompt_for_value("OPENAI_API_KEY") == "sk-typed"
    assert seen["OPENAI_API_KEY"]["hide_input"] is True
    # a plain flag has nothing to hide — echo it so the user can see typos
    seen.clear()
    assert cfg._prompt_for_value("SUB_AGENTS_ENABLED") == "sk-typed"
    assert seen["SUB_AGENTS_ENABLED"]["hide_input"] is False


# --- `config unset` -----------------------------------------------------------
# The counterpart `set` always had to have: a malformed/stale credential could
# only be REMOVED by hand-editing the env file (doctor said "present but
# unusable" and offered no verb). Same scoping rules as `set`.


def test_config_unset_removes_key_and_keeps_others(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    env = home / ".polyrob" / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-bad\nDEFAULT_PROVIDER=zai-coding\n")
    env.chmod(0o600)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["unset", "ANTHROPIC_API_KEY", "--global"])
    assert res.exit_code == 0, res.output
    text = env.read_text()
    assert "ANTHROPIC_API_KEY" not in text
    assert "DEFAULT_PROVIDER=zai-coding" in text
    assert env.stat().st_mode & 0o777 == 0o600


def test_config_unset_handles_spaced_key(tmp_path, monkeypatch):
    # A hand-edited `KEY = value` line (spaces around `=`) must still be found
    # and removed — same whitespace-normalized matching `set` uses.
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    env = home / ".polyrob" / ".env"
    env.write_text("ANTHROPIC_API_KEY = sk-bad\n")
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["unset", "ANTHROPIC_API_KEY", "--global"])
    assert res.exit_code == 0, res.output
    assert "ANTHROPIC_API_KEY" not in env.read_text()


def test_config_unset_missing_key_fails_honestly(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    (home / ".polyrob" / ".env").write_text("OPENAI_API_KEY=sk-x\n")
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["unset", "GEMINI_API_KEY", "--global"])
    assert res.exit_code != 0
    assert "not set" in res.output


def test_config_unset_points_at_the_other_scope(tmp_path, monkeypatch):
    # Key lives in the GLOBAL file but the user targeted the (default) project
    # scope — the error must say where it actually is instead of a bare miss.
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    (home / ".polyrob" / ".env").write_text("ANTHROPIC_API_KEY=sk-bad\n")
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["unset", "ANTHROPIC_API_KEY"])
    assert res.exit_code != 0
    assert "--global" in res.output


# --- `config migrate` ---------------------------------------------------------
# W1.1 (HANDOFF-env-system-and-key-subscriptions-2026-08-14): the explicit,
# one-time replacement for the retired automatic env-key backfill. Scans the
# legacy secret sources (root .env, config/.env.{production,development}) for
# secret-shaped keys absent from ~/.polyrob/.env and copies them on confirm.
# Lists key NAMES only — a value must never reach the terminal.


def test_config_migrate_all_copies_secret_keys_only(tmp_path, monkeypatch):
    home, proj = _isolated(tmp_path, monkeypatch)
    cfg = proj / "config"; cfg.mkdir()
    (cfg / ".env.production").write_text(
        "OPENROUTER_API_KEY=sk-or-migrate-123456\n"
        'PERPLEXITY_API_KEY="pplx-quoted-123456"\n'   # dotenv-quoted → stored unquoted
        "UVICORN_WORKERS=4\n"                          # flag: name not secret-shaped
        "REQUIRE_DEN_TOKEN=false\n"                    # _TOKEN suffix but flag-valued
        "MODEL_MAX_TOKENS=4096\n"                      # numeric flag
    )
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["migrate", "--all"])
    assert res.exit_code == 0, res.output
    text = (home / ".polyrob" / ".env").read_text()
    assert "OPENROUTER_API_KEY=sk-or-migrate-123456" in text
    assert "PERPLEXITY_API_KEY=pplx-quoted-123456" in text
    assert "UVICORN_WORKERS" not in text
    assert "REQUIRE_DEN_TOKEN" not in text
    assert "MODEL_MAX_TOKENS" not in text
    # names listed, values never shown
    assert "OPENROUTER_API_KEY" in res.output
    assert "sk-or-migrate-123456" not in res.output
    assert "pplx-quoted-123456" not in res.output
    assert (home / ".polyrob" / ".env").stat().st_mode & 0o777 == 0o600


def test_config_migrate_is_idempotent_never_overwrites(tmp_path, monkeypatch):
    home, proj = _isolated(tmp_path, monkeypatch)
    (home / ".polyrob" / ".env").write_text("OPENROUTER_API_KEY=sk-existing-9876543210\n")
    cfg = proj / "config"; cfg.mkdir()
    (cfg / ".env.production").write_text("OPENROUTER_API_KEY=sk-from-relic-123456\n")
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["migrate", "--all"])
    assert res.exit_code == 0, res.output
    assert "nothing to migrate" in res.output
    assert "sk-existing-9876543210" in (home / ".polyrob" / ".env").read_text()
    assert "sk-from-relic-123456" not in (home / ".polyrob" / ".env").read_text()


def test_config_migrate_confirms_each_key(tmp_path, monkeypatch):
    home, proj = _isolated(tmp_path, monkeypatch)
    cfg = proj / "config"; cfg.mkdir()
    (cfg / ".env.production").write_text(
        "OPENROUTER_API_KEY=sk-or-first-123456\n"
        "TWITTER_ACCESS_TOKEN=tw-second-123456\n"
    )
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["migrate"], input="y\nn\n")
    assert res.exit_code == 0, res.output
    text = (home / ".polyrob" / ".env").read_text()
    assert "OPENROUTER_API_KEY=sk-or-first-123456" in text
    assert "TWITTER_ACCESS_TOKEN" not in text


def test_config_migrate_nothing_to_migrate(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["migrate", "--all"])
    assert res.exit_code == 0, res.output
    assert "nothing to migrate" in res.output


def test_config_migrate_root_env_beats_config_env(tmp_path, monkeypatch):
    # Layering precedence must survive the copy: root .env is a HIGHER tier than
    # config/.env.production, so when both define a key, the root value is the
    # effective one and is the one that must land in ~/.polyrob/.env.
    home, proj = _isolated(tmp_path, monkeypatch)
    (proj / ".env").write_text("OPENAI_API_KEY=sk-root-0123456789\n")
    cfg = proj / "config"; cfg.mkdir()
    (cfg / ".env.production").write_text("OPENAI_API_KEY=sk-prod-0123456789\n")
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["migrate", "--all"])
    assert res.exit_code == 0, res.output
    assert "OPENAI_API_KEY=sk-root-0123456789" in (home / ".polyrob" / ".env").read_text()


def test_config_migrate_abort_names_all_flag(tmp_path, monkeypatch):
    # Ctrl-C / terminal EOF at the per-key prompt must not die with a bare
    # "Aborted!" — the remedy is --all, say so. (click.confirm raises Abort on
    # a real-terminal EOF; CliRunner's piped stdin can't produce that, so the
    # Abort is injected directly.)
    import click as _click
    home, proj = _isolated(tmp_path, monkeypatch)
    cfg = proj / "config"; cfg.mkdir()
    (cfg / ".env.production").write_text("OPENROUTER_API_KEY=sk-or-x-123456\n")

    def raise_abort(*_a, **_k):
        raise _click.exceptions.Abort()

    monkeypatch.setattr(_click, "confirm", raise_abort)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["migrate"])
    assert res.exit_code != 0
    assert "--all" in res.output
    assert "OPENROUTER_API_KEY" not in (home / ".polyrob" / ".env").read_text() \
        if (home / ".polyrob" / ".env").exists() else True


# --- `config path` — env-tier legibility (W1.4 + env-scheme unification) ------
# `config path` derives from the env_file_candidates SSOT (R-1), names what
# each tier is FOR, and surfaces a relic config/.env.* file the resolved env
# does not even read (the "why is my key ignored" trap).


def test_config_path_names_unread_production_relic(tmp_path, monkeypatch):
    home, proj = _isolated(tmp_path, monkeypatch)
    monkeypatch.delenv("CONFIG_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    cfg = proj / "config"; cfg.mkdir()
    (cfg / ".env.production").write_text("OPENROUTER_API_KEY=sk-or-x-123456\n")
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["path"])
    assert res.exit_code == 0, res.output
    # resolved env is development → .env.production is NOT read; say so + remedy
    assert "config/.env.production" in res.output.replace("\n", " ")
    assert "NOT read" in res.output
    assert "polyrob config migrate" in res.output


def test_config_path_lists_read_legacy_tier_with_purpose(tmp_path, monkeypatch):
    home, proj = _isolated(tmp_path, monkeypatch)
    monkeypatch.delenv("CONFIG_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    cfg = proj / "config"; cfg.mkdir()
    (cfg / ".env.development").write_text("OPENROUTER_API_KEY=sk-or-x-123456\n")
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["path"])
    assert res.exit_code == 0, res.output
    assert "config/.env.development" in res.output.replace("\n", " ")
    assert "server" in res.output          # names what the tier is FOR
    assert "polyrob config migrate" in res.output


def test_config_path_quiet_without_legacy_files(tmp_path, monkeypatch):
    home, proj = _isolated(tmp_path, monkeypatch)
    monkeypatch.delenv("CONFIG_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    from cli.commands.config import config as config_group
    res = CliRunner().invoke(config_group, ["path"])
    assert res.exit_code == 0, res.output
    # the two managed files are always shown...
    assert str(home / ".polyrob" / ".env") in res.output
    assert str(proj / ".polyrob" / ".env") in res.output
    # ...but absent legacy tiers are not listed as noise
    assert "config/.env" not in res.output
