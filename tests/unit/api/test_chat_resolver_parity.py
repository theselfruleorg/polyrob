"""Parity: chat_once (server) and `polyrob run` (CLI) resolve the same provider for the
same env, and the server consumers never read ~/.rob/cli.json (Phase 2)."""
import inspect


def test_chat_and_cli_agree_on_first_keyed_provider(monkeypatch):
    import cli.config_store as cs
    from agents.task_agent_lite import _resolve_chat_runtime

    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    cli_prov, _ = cs.resolve_provider_model(None, None, available_keys={"OPENROUTER_API_KEY"})
    # A well-formed key value (>=20 chars): the chat path validates the VALUE (mirrors
    # BotConfig), so a stub like "x" would (correctly) be rejected as unusable.
    chat_prov, _ = _resolve_chat_runtime(env={"OPENROUTER_API_KEY": "sk-or-realkey-0123456789"})
    assert cli_prov == chat_prov == "openrouter"


def test_server_consumers_use_core_resolver_not_cli():
    # The server→cli import smell is gone: these modules resolve via core.runtime_config.
    import api.openai_compat.model_map as mm
    import cron.runner as cr
    import agents.task.goals.dispatcher as disp
    for mod in (mm, cr, disp):
        src = inspect.getsource(mod)
        assert "core.runtime_config" in src, mod.__name__
        assert "from cli.config_store import resolve_provider_model" not in src, mod.__name__


def test_core_resolver_has_no_home_dir_read():
    # core.runtime_config must not READ ~/.rob/cli.json itself — it is injected by
    # the CLI. (Docstrings may mention cli.json; we check for actual code coupling.)
    import core.runtime_config as rc
    src = inspect.getsource(rc)
    assert "import cli" not in src
    assert "get_default_model" not in src  # the cli.json reader lives in cli/, not here
