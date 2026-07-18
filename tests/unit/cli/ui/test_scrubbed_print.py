"""Direct Rich prints in the REPL handlers must scrub secrets (review S2).

``CommandContext.emit`` is the documented choke point that scrubs command
output, but the handlers' Rich-table branches printed straight to the console
— bypassing the scrub. ``handlers._print_scrubbed`` is emit's renderable-shaped
twin: strings are scrubbed whole, Table cells in place; and no handler may call
``console.print`` directly anymore (source-pinned).
"""
from rich.console import Console
from rich.table import Table


def _capture(renderable):
    from cli.ui.commands import handlers
    console = Console(record=True, width=200)
    handlers._print_scrubbed(console, renderable)
    return console.export_text()


def test_table_cells_are_scrubbed():
    t = Table("key", "value")
    t.add_row("slack", "xoxb-123456789012-abcdefKLMNOP")
    out = _capture(t)
    assert "xoxb-" not in out
    assert "«redacted»" in out


def test_plain_string_is_scrubbed():
    out = _capture("header Authorization: Bearer eyJabc.def1234567890.sig-part")
    assert "eyJabc" not in out
    assert "«redacted»" in out


def test_non_secret_renderable_passes_through():
    t = Table("key", "value")
    t.add_row("model", "grok-4.3")
    out = _capture(t)
    assert "grok-4.3" in out


def test_handlers_never_print_raw():
    """The invariant, source-pinned: every direct Rich print routes through
    _print_scrubbed (its param is deliberately not named ``console``)."""
    import inspect

    import cli.ui.commands.handlers as handlers
    src = inspect.getsource(handlers)
    assert "console.print(" not in src
