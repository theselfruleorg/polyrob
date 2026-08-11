"""T3 — `polyrob serve` console subcommand.

The server gains a console-subcommand entry point alongside the legacy
``python main.py`` systemd launcher. Both share ONE uvicorn-launch callable
(``main.run_server``) — no duplicated logic.
"""

from click.testing import CliRunner


def test_serve_invokes_uvicorn(monkeypatch):
    """`polyrob serve --port 0` dispatches to uvicorn.run with the API app target."""
    calls = {}

    def fake_uvicorn_run(app_target, *args, **kwargs):
        calls["app_target"] = app_target
        calls["kwargs"] = kwargs

    # run_server does `import uvicorn; uvicorn.run(...)`, so patch the attribute
    # where it is looked up.
    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    # Deterministic preflight: a valid key present + no dependency on the machine's
    # config/.env (serve now gates on usable_providers_with_keys, so a valid-length
    # key is required to reach uvicorn).
    monkeypatch.setattr("core.bootstrap.load_env", lambda **k: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "a" * 32)

    from cli.commands.serve import serve

    runner = CliRunner()
    result = runner.invoke(serve, ["--port", "0"])

    assert result.exit_code == 0, result.output
    assert calls, "uvicorn.run was never called"
    assert calls["app_target"] == "api.app:get_app"
    assert calls["kwargs"]["host"] == "127.0.0.1"
    assert calls["kwargs"]["port"] == 0


def test_serve_rejects_malformed_key(monkeypatch):
    """A present-but-too-short key must NOT pass serve's preflight (it would boot
    uvicorn then crash in the lifespan). serve exits 1 without launching."""
    monkeypatch.setattr("core.bootstrap.load_env", lambda **k: None)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
              "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "short")  # present but < 20 chars
    ran = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: ran.setdefault("x", True))

    from cli.commands.serve import serve
    res = CliRunner().invoke(serve, ["--port", "0"])
    assert res.exit_code == 1
    assert "x" not in ran  # never reached uvicorn


def test_serve_registered_on_cli_group():
    """`serve` is registered on the top-level polyrob CLI group."""
    from cli.polyrob import cli

    assert "serve" in cli.commands


def _combined_output(res):
    out = res.output
    try:
        out += res.stderr or ""
    except (AttributeError, ValueError):
        pass
    return out


def test_serve_gate_reachable_without_repo_root_main(monkeypatch):
    """Installed users have no importable repo-root `main` module (it ships in no
    wheel, and console scripts never have the CWD on sys.path). The no-key
    refusal must still print — the key gate runs before any server import."""
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "main", None)  # simulate: main.py absent
    monkeypatch.setattr("core.bootstrap.load_env", lambda **k: None)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
              "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    from cli.commands.serve import serve

    res = CliRunner().invoke(serve, [])
    assert res.exit_code == 1
    assert "No API key found" in _combined_output(res), (
        f"expected the no-key refusal, got: {res.output!r} exc={res.exception!r}"
    )


def test_serve_boots_without_repo_root_main(monkeypatch):
    """With a usable key and no repo-root `main` module, serve still reaches
    uvicorn — run_server must live in an installed package, not main.py."""
    import sys as _sys

    calls = {}
    monkeypatch.setitem(_sys.modules, "main", None)
    monkeypatch.setattr("uvicorn.run", lambda target, *a, **k: calls.setdefault("target", target))
    monkeypatch.setattr("core.bootstrap.load_env", lambda **k: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "a" * 32)

    from cli.commands.serve import serve

    res = CliRunner().invoke(serve, ["--port", "0"])
    assert res.exit_code == 0, f"{res.output!r} exc={res.exception!r}"
    assert calls.get("target") == "api.app:get_app"


def test_run_server_lives_in_installed_package():
    """The uvicorn-launch callable is importable from the installed `api`
    package; repo-root main.py stays a thin shim over the same callable."""
    from api.server_boot import run_server

    import main as legacy_main

    assert legacy_main.run_server is run_server
