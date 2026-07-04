"""P0 Task 2 — shared child-env secret-scrub (extracted from local_subprocess)."""
from tools.code_exec.env_policy import SAFE_ALLOWLIST, SECRET_PAT, build_child_env


def test_allowlisted_host_vars_pass(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    env = build_child_env({})
    assert env.get("PATH") == "/usr/bin"
    assert env.get("LANG") == "en_US.UTF-8"


def test_secret_named_host_var_never_passes(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = build_child_env({})
    assert "OPENAI_API_KEY" not in env
    assert "PATH" in env  # non-secret allowlisted var still present


def test_extra_secret_named_key_is_stripped():
    env = build_child_env({"MY_SECRET_TOKEN": "nope", "FOO": "bar"})
    assert "MY_SECRET_TOKEN" not in env
    assert env["FOO"] == "bar"


def test_non_allowlisted_host_var_not_inherited(monkeypatch):
    monkeypatch.setenv("RANDOM_HOST_VAR", "leak")
    env = build_child_env({})
    assert "RANDOM_HOST_VAR" not in env


def test_secret_pat_matches_known_secret_names():
    for name in ("X_API_KEY", "DB_PASSWORD", "GH_TOKEN", "AWS_ACCESS_KEY",
                 "SEED", "MNEMONIC", "PRIVATE_KEY", "CREDENTIAL"):
        assert SECRET_PAT.search(name), name


def test_safe_allowlist_has_core_names():
    assert {"PATH", "HOME"} <= set(SAFE_ALLOWLIST)


def test_backend_build_env_delegates(monkeypatch):
    """LocalSubprocessBackend._build_env must produce the same result as build_child_env."""
    from tools.code_exec.backends.local_subprocess import LocalSubprocessBackend
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("PATH", "/bin")
    b = LocalSubprocessBackend()
    assert b._build_env({"FOO": "bar"}) == build_child_env({"FOO": "bar"})
    assert "OPENAI_API_KEY" not in b._build_env({})
