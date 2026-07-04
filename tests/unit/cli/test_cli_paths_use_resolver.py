import inspect
import cli.commands.chat as chat
import cli.commands.run as run


def test_chat_uses_shared_resolver():
    src = inspect.getsource(chat._repl_main)
    assert "resolve_provider_model" in src
    assert 'provider = "gemini"' not in src


def test_run_uses_shared_resolver():
    src = inspect.getsource(run)
    assert "resolve_provider_model" in src
    assert 'provider or "gemini"' not in src
