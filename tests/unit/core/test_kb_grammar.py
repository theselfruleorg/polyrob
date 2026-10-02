"""CLI7 (audit 2026-10-03): one `/kb` grammar for every seat."""
import pytest

from core.kb_grammar import LIST, SEARCH, USAGE, parse_kb_args


@pytest.mark.parametrize("args,action,collection,query", [
    ([], LIST, None, ""),
    (["list"], LIST, None, ""),
    (["list", "notes"], LIST, "notes", ""),
    (["LIST", "notes"], LIST, "notes", ""),
    (["search", "how", "to", "deploy"], SEARCH, None, "how to deploy"),
    (["x402", "services"], SEARCH, None, "x402 services"),
    (["mycoll"], SEARCH, None, "mycoll"),
    (["search"], USAGE, None, ""),
    (["list", "a", "b"], USAGE, None, ""),
])
def test_the_grammar(args, action, collection, query):
    cmd = parse_kb_args(args)
    assert (cmd.action, cmd.collection, cmd.query) == (action, collection, query)


def test_both_seats_import_the_one_parser():
    """A seat that parses `/kb` itself is the drift this module removed."""
    import inspect

    import cli.ui.commands.h_kb as repl
    import surfaces.telegram.harness as tg
    for mod in (repl, tg):
        assert "parse_kb_args" in inspect.getsource(mod), mod.__name__
