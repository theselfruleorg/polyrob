"""Bug A regression guard: Rich's ANSI must reach the terminal interpreted,
never as literal ``?[1;32m`` text, while the persistent prompt_toolkit box owns
the bottom of the screen.

Root cause (proven at the library source, ``prompt_toolkit/output/vt100.py``)::

    def write(self, data):     self._buffer.append(data.replace("\\x1b", "?"))  # ESC -> '?'
    def write_raw(self, data): self._buffer.append(data)                        # ESC survives

Under ``patch_stdout()`` the ``StdoutProxy`` flushes via ``output.write`` — so a
Rich ``Console(file=None)`` print (the agent speaker line + markdown) had every
``\\x1b`` rewritten to ``?``, leaving the bare SGR ``[1;32m`` as visible text
(the screenshot bug ``?[1;32m● ?[0m?[1mrob?[0m``).

The fix (``cli/commands/chat.py``) is ``patch_stdout(raw=True)`` → the proxy
flushes via ``output.write_raw`` → ANSI passes through and the terminal
interprets it. These tests pin BOTH halves of the contract:

1. behaviour — a styled Rich print survives as real ANSI under ``raw=True`` and
   demonstrably leaks under the default ``raw=False`` (so the guard is known to
   catch the bug), and
2. wiring — the persistent REPL call site actually passes ``raw=True`` (so a
   revert to a bare ``patch_stdout()`` is caught).

The end-to-end ``test_run_async_renders_box_submits_and_exits`` strips ANSI with
a regex before asserting, so it cannot see this leak — hence a dedicated guard.
"""

from __future__ import annotations

import asyncio
import inspect
import io

import pytest
from rich.console import Console

# Rich emits ``\x1b[1;32m`` (ESC + SGR) for the bold-green speaker dot. Two
# signatures distinguish a fixed render from the bug — note the prompt_toolkit
# Application ALWAYS emits its own framing escapes (real ``\x1b[`` cursor/mode
# sequences), so we must match the styled Rich CONTENT specifically, not the
# mere presence of any escape byte:
#   _REAL_SGR — the interpreted form (ESC preserved): the fix.
#   _LEAKED_SGR — the leaked form (ESC rewritten to '?' by output.write): the bug
#                 signature, exactly the screenshot ``?[1;32m●?[0m``.
_REAL_SGR = "\x1b[1;32m"
_LEAKED_SGR = "?[1;32m"


async def _styled_print_under_patch_stdout(raw: bool) -> str:
    """Run a real prompt_toolkit ``Application`` bound to a captured Vt100 sink,
    and — mid-run, under ``patch_stdout(raw=...)`` — print a styled Rich block
    exactly the way ``RichRenderer`` does (``Console(file=None)`` resolving the
    patched ``sys.stdout``). Returns the raw captured bytes.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.patch_stdout import patch_stdout

    sink = io.StringIO()
    with create_pipe_input() as pipe:
        out = Vt100_Output(sink, lambda: Size(rows=24, columns=90))
        with create_app_session(input=pipe, output=out):
            kb = KeyBindings()

            @kb.add("c-c")
            def _(event):  # pragma: no cover - safety exit
                event.app.exit()

            app = Application(
                layout=Layout(Window()),
                key_bindings=kb,
                output=out,
                input=pipe,
                full_screen=False,
            )

            async def driver() -> None:
                await asyncio.sleep(0.05)
                # file=None: Rich resolves the live (patched) sys.stdout, as the
                # real RichRenderer console does. force_terminal so color is on
                # regardless of the capture sink's isatty.
                console = Console(file=None, force_terminal=True, color_system="standard")
                console.print("[bold green]●[/] [bold]rob[/]")
                await asyncio.sleep(0.05)
                app.exit()

            with patch_stdout(raw=raw):
                task = asyncio.create_task(driver())
                try:
                    await asyncio.wait_for(app.run_async(), timeout=5)
                except (EOFError, asyncio.TimeoutError):
                    pass
                await task
    return sink.getvalue()


@pytest.mark.asyncio
async def test_styled_output_survives_as_real_ansi_under_raw_patch_stdout():
    """raw=True (the shipped fix): the styled Rich content reaches the sink as a
    real ESC-prefixed SGR, and the leaked ``?[1;32m`` form is absent."""
    out = await _styled_print_under_patch_stdout(raw=True)
    assert _REAL_SGR in out, "styled Rich content did not reach the sink as real ANSI"
    assert _LEAKED_SGR not in out, "ANSI leaked as literal text even under raw=True"


@pytest.mark.asyncio
async def test_default_patch_stdout_escapes_ansi_documents_the_bug():
    """raw=False (the default / the bug): the proxy rewrites ESC->'?', so the
    SGR survives as visible literal text. This demonstrates the guard above
    actually distinguishes fixed from broken."""
    out = await _styled_print_under_patch_stdout(raw=False)
    # The styled content's ESC was rewritten to '?', leaving the bare SGR as
    # literal, visible text (the screenshot leak) ...
    assert _LEAKED_SGR in out
    # ... and the interpreted form never reaches the sink for that content.
    assert _REAL_SGR not in out


def test_persistent_repl_wires_patch_stdout_raw_true():
    """Wiring guard: the persistent REPL must enter ``patch_stdout(raw=True)``.
    A revert to a bare ``patch_stdout()`` would silently resurrect bug A."""
    from cli.commands import chat

    src = inspect.getsource(chat._run_persistent_app)
    assert "patch_stdout(raw=True)" in src
    # No bare/default patch_stdout call that would escape ANSI.
    assert "patch_stdout()" not in src
