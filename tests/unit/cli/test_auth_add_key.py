"""W2 (HANDOFF-env-system-and-key-subscriptions-2026-08-14): `polyrob auth add`
handles KEY-BASED providers, not just OAuth rows. A row with an env_key and no
OAuth block gets a connect flow: signup_url + tos_note shown, hidden key
prompt, write to ~/.polyrob/.env, doctor-style echo, optional live validation.
The z.ai pair routes by PLAN choice (ZAI_API_KEY vs GLM_API_KEY — one key
works only on its own endpoint).
"""
from pathlib import Path

from click.testing import CliRunner


def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    return home, proj


def test_auth_add_key_based_writes_key(tmp_path, monkeypatch):
    home, _ = _isolated(tmp_path, monkeypatch)
    key = "csk-cerebras-key-0123456789abcdef"
    from cli.commands.auth import auth
    res = CliRunner().invoke(auth, ["add", "cerebras"], input=f"{key}\n")
    assert res.exit_code == 0, res.output
    text = (home / ".polyrob" / ".env").read_text()
    assert f"CEREBRAS_API_KEY={key}" in text
    assert (home / ".polyrob" / ".env").stat().st_mode & 0o777 == 0o600
    # signup + ToS surfaced, value never echoed
    assert "cloud.cerebras.ai" in res.output
    assert "flat-rate" in res.output          # tos_note text
    assert key not in res.output
    # doctor-style readiness echo
    assert "present" in res.output


def test_auth_add_key_subscription_row_shows_flat_rate_tag(tmp_path, monkeypatch):
    home, _ = _isolated(tmp_path, monkeypatch)
    key = "sk-zai-0123456789abcdefgh"
    from cli.commands.auth import auth
    res = CliRunner().invoke(auth, ["add", "zai-coding"], input=f"{key}\n")
    assert res.exit_code == 0, res.output
    assert f"ZAI_API_KEY={key}" in (home / ".polyrob" / ".env").read_text()
    assert "subscription" in res.output


def test_auth_add_zai_plan_choice_routes_the_key_var(tmp_path, monkeypatch):
    # The user named `zai-coding` but answers "pay-as-you-go" → the key must
    # land in GLM_API_KEY (the `zai` row): plan choice IS the routing fact.
    home, _ = _isolated(tmp_path, monkeypatch)
    monkeypatch.setattr("cli.commands.auth._stdin_isatty", lambda: True)
    key = "sk-glm-payg-0123456789abcdef"
    from cli.commands.auth import auth
    # input: plan choice "2", ToS confirm "y", key, validation ask "n"
    res = CliRunner().invoke(auth, ["add", "zai-coding"],
                             input=f"2\ny\n{key}\nn\n")
    assert res.exit_code == 0, res.output
    text = (home / ".polyrob" / ".env").read_text()
    assert f"GLM_API_KEY={key}" in text
    assert "ZAI_API_KEY" not in text


def test_auth_add_unknown_provider_errors(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    from cli.commands.auth import auth
    res = CliRunner().invoke(auth, ["add", "not-a-provider-xyz"])
    assert res.exit_code != 0
    assert "unknown provider" in res.output


def test_auth_add_oauth_row_still_gated_on_flag(tmp_path, monkeypatch):
    # An OAuth row keeps the existing flow and its LLM_OAUTH_ENABLED gate —
    # the key-based path must not bypass it.
    _isolated(tmp_path, monkeypatch)
    monkeypatch.delenv("LLM_OAUTH_ENABLED", raising=False)
    from cli.commands.auth import auth
    res = CliRunner().invoke(auth, ["add", "github-copilot"])
    assert res.exit_code != 0
    assert "LLM_OAUTH_ENABLED" in res.output


def test_auth_add_validate_rejection_is_fail_open(tmp_path, monkeypatch):
    # A provider 401 on the live probe must WARN with the remedy, not unwrite
    # the key or fail the command (W2.2 fail-open contract).
    home, _ = _isolated(tmp_path, monkeypatch)
    monkeypatch.setattr("cli.commands.auth._probe_key",
                        lambda spec, key: (False, "HTTP 401: invalid api key"))
    key = "sk-zai-0123456789abcdefgh"
    from cli.commands.auth import auth
    res = CliRunner().invoke(auth, ["add", "zai-coding", "--validate"],
                             input=f"{key}\n")
    assert res.exit_code == 0, res.output
    assert f"ZAI_API_KEY={key}" in (home / ".polyrob" / ".env").read_text()
    assert "401" in res.output
    assert "polyrob config unset ZAI_API_KEY --global" in res.output


def test_auth_add_validate_success(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    monkeypatch.setattr("cli.commands.auth._probe_key",
                        lambda spec, key: (True, "HTTP 200 from https://api.z.ai/api/anthropic"))
    from cli.commands.auth import auth
    res = CliRunner().invoke(auth, ["add", "zai-coding", "--validate"],
                             input="sk-zai-0123456789abcdefgh\n")
    assert res.exit_code == 0, res.output
    assert "verified" in res.output


# --- _probe_key classification (transport-aware, never raises) ----------------


class _FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def test_probe_key_anthropic_transport_and_classification(monkeypatch):
    from modules.llm.provider_spec import get_spec
    import cli.commands.auth as auth_mod

    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url; seen["headers"] = headers; seen["json"] = json
        return _FakeResp(401, "invalid key")

    monkeypatch.setattr("httpx.post", fake_post)
    spec = get_spec("zai-coding")
    ok, detail = auth_mod._probe_key(spec, "sk-zai-x")
    assert ok is False and "401" in detail
    assert seen["url"] == "https://api.z.ai/api/anthropic/v1/messages"
    assert seen["headers"]["Authorization"] == "Bearer sk-zai-x"   # bearer_auth row
    assert seen["json"]["max_tokens"] == 1


def test_probe_key_openai_transport_models_list(monkeypatch):
    from modules.llm.provider_spec import get_spec
    import cli.commands.auth as auth_mod

    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url; seen["headers"] = headers
        return _FakeResp(200)

    monkeypatch.setattr("httpx.get", fake_get)
    spec = get_spec("cerebras")
    ok, _detail = auth_mod._probe_key(spec, "csk-x")
    assert ok is True
    assert seen["url"] == "https://api.cerebras.ai/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer csk-x"


def test_probe_key_network_error_is_inconclusive(monkeypatch):
    from modules.llm.provider_spec import get_spec
    import cli.commands.auth as auth_mod

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("httpx.get", boom)
    ok, detail = auth_mod._probe_key(get_spec("cerebras"), "csk-x")
    assert ok is None
    assert "connection refused" in detail
