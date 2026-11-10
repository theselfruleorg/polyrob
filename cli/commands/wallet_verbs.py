"""`polyrob wallet send|swap|check` — the chat money verbs, for a script (113 G-1).

Each command runs the SAME handler the terminal chat runs for `/send`, `/swap` and
`/check`: the ``repl`` handler reference the verb row carries in ``core.money_verbs``
(``core.verbs.handler_ref``), called with a terminal command context. So the words,
the quote, the `go` rule and every gate are the chat verb's own:

* the words after the command are the verb's words, unchanged;
* without the word `go` it is a QUOTE (a guarded dry run) and nothing moves;
* with `go` the handler sends, and every refusal is the wallet tool's —
  ``tx_guard.authorize`` and the money kernel ``core.money.authorize`` — exactly as
  on the chat seat. This module adds no authority, no cap, no flag and no
  confirmation of its own; it parses nothing.

The agent cannot reach these from its own shell: ``cli/agent_child_gate.py`` refuses
every verb that is not a read inside a process the agent started (`check` is a read).

Exit status: 0 for an answer, 1 when the answer starts with ❌ (nothing was done).
"""
import asyncio
import importlib
import re
import sys

import click

from cli.commands._bootstrap import ensure_env_loaded
from cli.commands.wallet import wallet_cmd


class _Collect:
    """The renderer surface ``CommandContext.emit`` writes to: collect the text."""

    def __init__(self):
        self.blocks = []

    def print_block(self, text, *, title="", style=""):
        self.blocks.append(str(text))


def run_chat_verb(verb: str, words) -> str:
    """Run the terminal handler of ``verb`` (``/send`` …) on ``words``; its text."""
    ensure_env_loaded()
    from core.verbs import handler_ref
    ref = handler_ref("repl", verb)
    if ref is None:
        raise click.ClickException(
            f"{verb} has no terminal handler on this install, so nothing was done.")
    module, attr = ref.split(":", 1)
    handler = getattr(importlib.import_module(module), attr)
    from cli.ui.commands.registry import CommandContext
    from core.identity import resolve_identity
    out = _Collect()
    ctx = CommandContext(renderer=out, user_id=resolve_identity(), args=list(words),
                         raw=" ".join([verb.lstrip("/"), *words]))
    result = handler(ctx)
    if asyncio.iscoroutine(result):
        asyncio.run(result)
    return "\n".join(out.blocks)


#: The handler names its next step in chat words (``To send it: /send … go``); here the
#: same words are a command line. Display only: the words themselves are unchanged.
_CHAT_FORM = re.compile(r"(^|[\s:(])/(send|swap|check)(?= )", re.M)


def _answer(verb: str, words) -> None:
    text = _CHAT_FORM.sub(r"\1polyrob wallet \2", run_chat_verb(verb, words))
    click.echo(text)
    if text.lstrip().startswith("❌"):
        sys.exit(1)


_CTX = {"ignore_unknown_options": True}


@wallet_cmd.command("send", context_settings=_CTX)
@click.argument("words", nargs=-1, type=click.UNPROCESSED)
def send_cmd(words):
    """Send native value or a token: a quote; add the word `go` to send.

    The words of chat `/send`: `<amount|$usd> <native|token> to <address> on <chain> [go]`.
    """
    _answer("/send", words)


@wallet_cmd.command("swap", context_settings=_CTX)
@click.argument("words", nargs=-1, type=click.UNPROCESSED)
def swap_cmd(words):
    """Swap one token for another: a quote; add the word `go` to swap.

    The words of chat `/swap`; the quote names the minimum you receive.
    """
    _answer("/swap", words)


@wallet_cmd.command("check", context_settings=_CTX)
@click.argument("words", nargs=-1, type=click.UNPROCESSED)
def check_cmd(words):
    """Look at any address or ticker: a wallet's holdings or a token's report (a read)."""
    _answer("/check", words)
