"""`polyrob surfaces list | add | probe` (064 S2b F6). Offline: every probe's HTTP
is stubbed at ``surfaces._probe.http_json``."""
import os

import pytest
from click.testing import CliRunner

SECRET = "123456:AAH-very-secret-token-value"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for k in list(os.environ):
        if k.startswith(("TELEGRAM_", "DISCORD_", "SLACK_", "SIGNAL_", "WHATSAPP_",
                         "TWITTER_", "GMAIL_", "AGENTMAIL_", "X_SURFACE", "EMAIL_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("cli.commands.surfaces._load_env", lambda: dict(os.environ))
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    return tmp_path


def _run(*args, input=None):
    from cli.polyrob import cli
    return CliRunner().invoke(cli, ["surfaces", *args], input=input)


def test_registered_and_listed_in_the_surfaces_help_group():
    from cli.polyrob import _HELP_GROUPS, cli
    assert "surfaces" in cli.list_commands(None)
    assert "surfaces" in dict(_HELP_GROUPS)["Surfaces"]


def test_list_shows_every_catalog_row(env, monkeypatch):
    from core.surfaces.catalog import surface_ids
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    monkeypatch.setenv("TELEGRAM_SURFACE_ENABLED", "true")
    res = _run("list")
    assert res.exit_code == 0, res.output
    for sid in surface_ids():
        assert sid in res.output
    assert "missing DISCORD_BOT_TOKEN" in res.output
    assert SECRET not in res.output


def test_probe_ok_failed_and_unavailable(env, monkeypatch):
    calls = []

    async def _http(method, url, **kw):
        calls.append(url)
        return 200, {"ok": True, "result": {"username": "rob_bot"}}, ""

    monkeypatch.setattr("surfaces._probe.http_json", _http)
    monkeypatch.setattr("surfaces.telegram.probe.http_json", _http)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    res = _run("probe", "telegram")
    assert res.exit_code == 0 and "ok — @rob_bot" in res.output
    assert SECRET not in res.output

    async def _refused(method, url, **kw):
        return 401, {"ok": False}, ""

    monkeypatch.setattr("surfaces.telegram.probe.http_json", _refused)
    res = _run("probe", "telegram")
    assert res.exit_code == 2 and "failed" in res.output

    async def _down(method, url, **kw):
        return None, None, "ClientConnectorError"

    monkeypatch.setattr("surfaces.telegram.probe.http_json", _down)
    res = _run("probe", "telegram")
    assert res.exit_code == 1 and "unavailable(ClientConnectorError)" in res.output
    assert SECRET not in res.output


def test_an_absent_credential_is_unavailable_not_a_zero(env):
    res = _run("probe", "discord")
    assert res.exit_code == 1
    assert "unavailable(missing DISCORD_BOT_TOKEN)" in res.output


def test_probe_never_sends(env, monkeypatch):
    """Every shipped probe issues reads only: no send / post-message endpoint."""
    seen = []

    async def _http(method, url, **kw):
        seen.append((method, url))
        return 500, None, ""

    from core.surfaces.catalog import surfaces as _catalog
    for spec in _catalog():   # core rows + a loaded pack's (067 P3b: X from the x pack)
        try:
            monkeypatch.setattr(f"{spec.module}.probe.http_json", _http)
        except (ImportError, AttributeError):
            continue          # a surface without an HTTP probe
    for k, v in {"TELEGRAM_BOT_TOKEN": "t", "DISCORD_BOT_TOKEN": "d",
                 "SLACK_BOT_TOKEN": "b", "SLACK_APP_TOKEN": "a", "SIGNAL_ACCOUNT": "+1",
                 "WHATSAPP_ACCESS_TOKEN": "w", "WHATSAPP_PHONE_NUMBER_ID": "9",
                 "TWITTER_OAUTH2_ACCESS_TOKEN": "o", "AGENTMAIL_API_KEY": "m"}.items():
        monkeypatch.setenv(k, v)
    _run("probe", "all")
    assert seen, "no probe ran"
    for method, url in seen:
        assert not any(w in url.lower() for w in ("sendmessage", "/messages", "chat.post",
                                                  "/send", "tweets")), url


def test_add_writes_through_the_env_writer_and_never_echoes(env, monkeypatch):
    async def _http(method, url, **kw):
        return 200, {"username": "rob", "flags": 1 << 19}, ""

    monkeypatch.setattr("surfaces.discord.probe.http_json", _http)
    res = _run("add", "discord", input=f"{SECRET}\n")
    assert res.exit_code == 0, res.output
    assert SECRET not in res.output
    target = env / "home" / ".env"
    text = target.read_text()
    assert f"DISCORD_BOT_TOKEN={SECRET}" in text
    assert "DISCORD_SURFACE_ENABLED=true" in text
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert "probe: ok" in res.output


def test_add_refuses_a_value_that_would_smuggle_a_second_line(env, monkeypatch):
    # A terminal prompt splits on the line break itself; a pasted value can
    # still carry a bare CR, so inject the raw value past the prompt.
    monkeypatch.setattr("click.prompt", lambda *a, **k: "tok\rPOLYROB_LOCAL=true")
    res = _run("add", "discord", "--no-probe")
    assert "must not contain CR, LF or NUL" in res.output
    target = env / "home" / ".env"
    assert res.exit_code != 0
    assert not target.exists() or "POLYROB_LOCAL" not in target.read_text()


def test_webhook_surface_prints_the_url_to_register(env):
    res = _run("add", "whatsapp", "--no-probe", "--no-enable", input="\n\n\n\n")
    assert res.exit_code == 0, res.output
    assert "/webhooks/whatsapp" in res.output


def test_unknown_surface_is_named(env):
    res = _run("probe", "nope")
    assert res.exit_code != 0 and "unknown surface" in res.output


# --- the deployed box (031 deployed-home rule) -------------------------------------

@pytest.fixture
def deployed(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for k in list(os.environ):
        if k.startswith(("TELEGRAM_", "DISCORD_", "SLACK_", "SIGNAL_", "WHATSAPP_",
                         "TWITTER_", "GMAIL_", "AGENTMAIL_", "X_SURFACE", "EMAIL_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("core.bootstrap.load_env", lambda **k: None)
    f = tmp_path / "polyrob.env"
    f.write_text(f"TELEGRAM_BOT_TOKEN={SECRET}\nexport DISCORD_BOT_TOKEN='d-tok'\n")
    monkeypatch.setattr("core.admin_data_home.DEPLOYED_ENV_FILE", str(f))
    return f


def test_list_reads_the_deployed_env_file(deployed):
    res = _run("list")
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    tg = lines[lines.index(next(l for l in lines if l.startswith("telegram"))) + 1]
    dc = lines[lines.index(next(l for l in lines if l.startswith("discord"))) + 1]
    assert "credentials: yes" in tg and "credentials: yes" in dc
    assert SECRET not in res.output


def test_an_unreadable_deployed_env_is_cannot_tell_not_missing(deployed):
    deployed.chmod(0)
    try:
        if os.access(deployed, os.R_OK):
            pytest.skip("running as root: the file stays readable")
        res = _run("list")
        assert "cannot tell" in res.output and "sudo" in res.output
        assert "missing TELEGRAM_BOT_TOKEN" not in res.output
    finally:
        deployed.chmod(0o600)


def test_add_on_a_deployed_box_writes_the_service_env_file(deployed, monkeypatch):
    async def _http(method, url, **kw):
        return 200, {"username": "rob"}, ""

    monkeypatch.setattr("surfaces.discord.probe.http_json", _http)
    res = _run("add", "discord", input="new-tok\n")
    assert res.exit_code == 0, res.output
    assert "DISCORD_BOT_TOKEN=new-tok" in deployed.read_text()
    assert f"TELEGRAM_BOT_TOKEN={SECRET}" in deployed.read_text()   # the rest is kept


def test_running_names_a_standalone_process(monkeypatch):
    from cli.commands import surfaces as cmd
    from core.surfaces.catalog import get
    monkeypatch.setattr("cli.update.process_guard._iter_cmdlines",
                        lambda: iter([(41, ["/opt/polyrob/venv/bin/polyrob", "telegram"]),
                                      (42, ["/usr/bin/python3", "other.py"])]))
    assert cmd._running(get("telegram")) == "polyrob telegram (pid 41)"
    assert cmd._running(get("slack")) == ""
