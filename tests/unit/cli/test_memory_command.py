from cli.ui.commands import build_default_registry


def test_memory_command_registered():
    reg = build_default_registry()
    # CommandRegistry.commands() returns a list[Command]; names carry no leading slash.
    names = {cmd.name for cmd in reg.commands()}
    assert any(n in ("/memory", "memory") for n in names)
    # the command resolves and routes to a handler
    assert reg.lookup("/memory") is not None
