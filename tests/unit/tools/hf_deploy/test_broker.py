"""HFSpacesBroker — token custody + injectable HfApi/http seams (zero network)."""
import asyncio

import pytest

from tests.unit.tools.hf_deploy.conftest import FakeHfApi


def _broker(api_factory=None, http_get=None, **kw):
    from tools.hf_deploy.broker import HFSpacesBroker
    kw.setdefault("runtime_wait_sec", 2.0)
    kw.setdefault("poll_interval_sec", 0.0)
    return HFSpacesBroker(api_factory=api_factory, http_get=http_get, **kw)


def test_resolve_token_reads_env_at_call_time(monkeypatch):
    from tools.hf_deploy.broker import HFSpacesBroker
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert HFSpacesBroker.resolve_token() is None
    monkeypatch.setenv("HF_TOKEN", "  hf_tok  ")
    assert HFSpacesBroker.resolve_token() == "hf_tok"


def test_deploy_space_drives_create_upload_and_runtime(monkeypatch, tmp_path, deploy_env):
    apis = []

    def factory(token):
        api = FakeHfApi(token)
        apis.append(api)
        return api

    broker = _broker(api_factory=factory)
    url = asyncio.run(broker.deploy_space(
        space_repo="test-org/my-app", workspace_dir=str(tmp_path)))
    assert url == "https://test-org-my-app.hf.space"
    api = apis[0]
    assert api.token == deploy_env  # token flows ONLY into the injected factory
    names = [c[0] for c in api.calls]
    assert names[0] == "create_repo"
    assert "upload_folder" in names
    assert "get_space_runtime" in names
    create = api.calls[0]
    assert create[1] == "test-org/my-app" and create[2] == "space" and create[3] == "docker"


def test_deploy_space_sets_declared_space_secrets(monkeypatch, tmp_path, deploy_env):
    api_holder = {}

    def factory(token):
        api_holder["api"] = FakeHfApi(token)
        return api_holder["api"]

    broker = _broker(api_factory=factory)
    asyncio.run(broker.deploy_space(
        space_repo="test-org/my-app", workspace_dir=str(tmp_path),
        secrets={"APP_KEY": "v1"}))
    assert ("add_space_secret", "test-org/my-app", "APP_KEY", "v1") in api_holder["api"].calls


def test_deploy_space_without_token_raises_clear_error(monkeypatch, tmp_path):
    from tools.hf_deploy.broker import BrokerError
    monkeypatch.delenv("HF_TOKEN", raising=False)
    broker = _broker(api_factory=lambda t: FakeHfApi(t))
    with pytest.raises(BrokerError, match="HF_TOKEN"):
        asyncio.run(broker.deploy_space(space_repo="o/a", workspace_dir=str(tmp_path)))


def test_deploy_space_waits_through_building_to_running(monkeypatch, tmp_path, deploy_env):
    api = {}

    def factory(token):
        api["api"] = FakeHfApi(token, stages=["BUILDING", "BUILDING", "RUNNING"])
        return api["api"]

    broker = _broker(api_factory=factory)
    url = asyncio.run(broker.deploy_space(space_repo="o/a", workspace_dir=str(tmp_path)))
    assert url


def test_deploy_space_terminal_bad_stage_raises(monkeypatch, tmp_path, deploy_env):
    from tools.hf_deploy.broker import BrokerError

    def factory(token):
        return FakeHfApi(token, stages=["BUILD_ERROR"])

    broker = _broker(api_factory=factory)
    with pytest.raises(BrokerError, match="BUILD_ERROR"):
        asyncio.run(broker.deploy_space(space_repo="o/a", workspace_dir=str(tmp_path)))


def test_broker_errors_never_contain_the_token(monkeypatch, tmp_path, deploy_env):
    from tools.hf_deploy.broker import BrokerError
    token = deploy_env

    def factory(tok):
        return FakeHfApi(tok, fail_upload=RuntimeError(
            f"401 unauthorized for token {token} on upload"))

    broker = _broker(api_factory=factory)
    with pytest.raises(BrokerError) as ei:
        asyncio.run(broker.deploy_space(space_repo="o/a", workspace_dir=str(tmp_path)))
    assert token not in str(ei.value)
    assert "401" in str(ei.value)


def test_broker_scrubs_declared_secret_values_from_errors(monkeypatch, tmp_path, deploy_env):
    """A raising add_space_secret must not echo the secret VALUE it was setting
    into the BrokerError (which flows into ActionResult.error -> agent memory)."""
    from tools.hf_deploy.broker import BrokerError
    secret_value = "sk-SUPERSECRETAPPKEY-xyz"

    class SecretFailApi(FakeHfApi):
        def add_space_secret(self, repo_id=None, key=None, value=None):
            raise RuntimeError(f"500 error while setting secret value={value}")

    broker = _broker(api_factory=lambda tok: SecretFailApi(tok))
    with pytest.raises(BrokerError) as ei:
        asyncio.run(broker.deploy_space(
            space_repo="o/a", workspace_dir=str(tmp_path),
            secrets={"APP_KEY": secret_value}))
    assert secret_value not in str(ei.value)
    assert "500" in str(ei.value)


def test_broker_scrubs_token_from_api_construction_error(monkeypatch, tmp_path, deploy_env):
    """A token-bearing exception from HfApi CONSTRUCTION (inside _make_api) must
    be scrubbed too — _make_api is inside the same scrub try/except."""
    from tools.hf_deploy.broker import BrokerError
    token = deploy_env

    def factory(tok):
        raise RuntimeError(f"401 bad token {tok} at construction")

    broker = _broker(api_factory=factory)
    with pytest.raises(BrokerError) as ei:
        asyncio.run(broker.deploy_space(space_repo="o/a", workspace_dir=str(tmp_path)))
    assert token not in str(ei.value)
    assert "401" in str(ei.value)


def test_missing_huggingface_hub_yields_clear_error(monkeypatch, tmp_path, deploy_env):
    """api_factory=None -> lazy import; simulate the optional dep being absent."""
    import builtins
    from tools.hf_deploy.broker import BrokerError

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("huggingface_hub"):
            raise ImportError("No module named 'huggingface_hub'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    broker = _broker(api_factory=None)
    with pytest.raises(BrokerError, match="huggingface_hub"):
        asyncio.run(broker.deploy_space(space_repo="o/a", workspace_dir=str(tmp_path)))


def test_health_check_2xx_true_other_false(deploy_env):
    calls = []

    def getter(url, timeout):
        calls.append(url)
        return 200 if url.endswith("/ok") else 503

    broker = _broker(api_factory=lambda t: FakeHfApi(t), http_get=getter)
    assert asyncio.run(broker.health_check("https://o-a.hf.space/ok")) is True
    assert asyncio.run(broker.health_check("https://o-a.hf.space/bad")) is False
    assert calls == ["https://o-a.hf.space/ok", "https://o-a.hf.space/bad"]


def test_delete_space_calls_delete_repo(monkeypatch, deploy_env):
    holder = {}

    def factory(token):
        holder["api"] = FakeHfApi(token)
        return holder["api"]

    broker = _broker(api_factory=factory)
    asyncio.run(broker.delete_space(space_repo="test-org/my-app"))
    assert ("delete_repo", "test-org/my-app", "space") in holder["api"].calls


def test_space_url_slugs_owner_and_name():
    from tools.hf_deploy.broker import HFSpacesBroker
    assert HFSpacesBroker.space_url("My_Org/some.app") == "https://my-org-some-app.hf.space"


def test_deploy_refuses_workspace_with_env_file(monkeypatch, tmp_path, deploy_env):
    """P1 (finalization): a workspace carrying a credential file must NOT be
    published to a public Space — refuse before any upload."""
    (tmp_path / "app.py").write_text("print('hi')")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-whatever")

    apis = []

    def factory(token):
        api = FakeHfApi(token)
        apis.append(api)
        return api

    broker = _broker(api_factory=factory)
    with pytest.raises(Exception) as ei:
        asyncio.run(broker.deploy_space(space_repo="o/a", workspace_dir=str(tmp_path)))
    assert ".env" in str(ei.value)
    # Never uploaded — the refusal is pre-network.
    if apis:
        assert "upload_folder" not in [c[0] for c in apis[0].calls]


def test_deploy_refuses_secret_shape_in_content(monkeypatch, tmp_path, deploy_env):
    (tmp_path / "settings.py").write_text("GEMINI_API_KEY=abcdef1234567890abcdef")
    broker = _broker(api_factory=lambda t: FakeHfApi(t))
    with pytest.raises(Exception) as ei:
        asyncio.run(broker.deploy_space(space_repo="o/a", workspace_dir=str(tmp_path)))
    assert "settings.py" in str(ei.value)


def test_deploy_allows_clean_workspace(monkeypatch, tmp_path, deploy_env):
    (tmp_path / "app.py").write_text("print('hello world')")
    (tmp_path / "README.md").write_text("# my app\nA normal project.")
    apis = []
    broker = _broker(api_factory=lambda t: apis.append(FakeHfApi(t)) or apis[-1])
    url = asyncio.run(broker.deploy_space(space_repo="o/a", workspace_dir=str(tmp_path)))
    assert url.endswith(".hf.space")
    assert "upload_folder" in [c[0] for c in apis[0].calls]
