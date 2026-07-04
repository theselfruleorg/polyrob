"""Tests for T16: interrupt-and-redirect (Ctrl-C mid-turn → redirect instead of abort).

Flag: INTERRUPT_REDIRECT (default OFF).

Mirrors the injectable read_line / convo seam from test_repl.py.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock


def _reader(lines):
    """Async read_line that yields lines in order, then raises EOFError."""
    it = iter(lines)

    async def read_line():
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    return read_line


# ---------------------------------------------------------------------------
# Flag OFF — byte-identical to existing behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_off_ctrl_c_aborts_no_redirect_prompt(monkeypatch, capsys):
    """Flag OFF: in-turn KeyboardInterrupt aborts the turn, no redirect prompt."""
    monkeypatch.setenv("INTERRUPT_REDIRECT", "false")

    from cli.commands.chat import _conversation_loop

    convo = MagicMock()
    convo.respond = AsyncMock(side_effect=[KeyboardInterrupt, "ok"])

    # Only two lines: first raises KI (aborted), second succeeds.
    await _conversation_loop(
        convo,
        MagicMock(),
        read_line=_reader(["task one", "task two"]),
    )

    out = capsys.readouterr().out
    # Existing abort message must appear
    assert "Turn interrupted" in out
    # The redirect prompt must NOT appear
    assert "redirect" not in out.lower()
    # Second turn still ran
    assert convo.respond.await_count == 2


@pytest.mark.asyncio
async def test_flag_off_no_injection_on_abort(monkeypatch):
    """Flag OFF: convo's set_turn_input is never called when a turn is interrupted."""
    monkeypatch.setenv("INTERRUPT_REDIRECT", "false")

    from cli.commands.chat import _conversation_loop

    convo = MagicMock()
    convo.respond = AsyncMock(side_effect=KeyboardInterrupt)
    agent = MagicMock()
    convo.agent = agent

    await _conversation_loop(
        convo,
        MagicMock(),
        read_line=_reader(["first"]),
    )

    agent.set_turn_input.assert_not_called()


# ---------------------------------------------------------------------------
# Flag ON — redirect path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_on_redirect_used_as_next_turn(monkeypatch, capsys):
    """Flag ON: non-empty redirect becomes the next turn input."""
    monkeypatch.setenv("INTERRUPT_REDIRECT", "true")

    from cli.commands.chat import _conversation_loop

    convo = MagicMock()
    # First respond() raises KI; second (redirect turn) succeeds.
    convo.respond = AsyncMock(side_effect=[KeyboardInterrupt, "redirect reply"])

    # read_line sequence: original task, then redirect text.
    await _conversation_loop(
        convo,
        MagicMock(),
        read_line=_reader(["original task", "new direction"]),
    )

    out = capsys.readouterr().out
    assert "Turn interrupted" in out
    assert "Redirecting" in out
    # The redirect text must have been passed to respond() as the next turn.
    assert convo.respond.await_count == 2
    call_args = [call.args[0] for call in convo.respond.call_args_list]
    assert "new direction" in call_args


@pytest.mark.asyncio
async def test_flag_on_blank_redirect_cancels_no_injection(monkeypatch, capsys):
    """Flag ON + blank redirect: abort (no injection), no duplicate abort message."""
    monkeypatch.setenv("INTERRUPT_REDIRECT", "true")

    from cli.commands.chat import _conversation_loop

    convo = MagicMock()
    convo.respond = AsyncMock(side_effect=[KeyboardInterrupt, "ok"])

    # read_line: original task → KI in respond; blank redirect; next real turn.
    await _conversation_loop(
        convo,
        MagicMock(),
        read_line=_reader(["first task", "", "second task"]),
    )

    out = capsys.readouterr().out
    assert "Turn interrupted" in out
    # Blank redirect = cancel: second task runs normally.
    assert convo.respond.await_count == 2
    call_args = [call.args[0] for call in convo.respond.call_args_list]
    # "first task" was attempted (raised KI); "second task" ran.
    assert "first task" in call_args
    assert "second task" in call_args


@pytest.mark.asyncio
async def test_flag_on_redirect_prompt_eoferror_falls_back_to_abort(monkeypatch, capsys):
    """Flag ON: EOFError in the redirect read_line falls back to abort (fail-open).

    The redirect read_line call is wrapped in a try/except so an EOFError (e.g. the
    input stream closed) is treated as blank → abort. The turn-interrupted message
    still prints and the loop exits cleanly.
    """
    monkeypatch.setenv("INTERRUPT_REDIRECT", "true")

    from cli.commands.chat import _conversation_loop

    convo = MagicMock()
    convo.respond = AsyncMock(side_effect=KeyboardInterrupt)

    call_count = {"n": 0}

    async def _eof_on_redirect():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "first task"
        # Second call is the redirect prompt read — simulate EOF.
        raise EOFError

    await _conversation_loop(
        convo,
        MagicMock(),
        read_line=_eof_on_redirect,
    )

    out = capsys.readouterr().out
    # Should print "Turn interrupted" (the abort path inside the redirect branch).
    assert "Turn interrupted" in out
    # Should NOT print "Redirecting" (blank/EOF → cancel).
    assert "Redirecting" not in out


@pytest.mark.asyncio
async def test_flag_on_redirect_prompt_uses_read_line_seam(monkeypatch):
    """Flag ON: the redirect prompt is read via the injectable read_line seam."""
    monkeypatch.setenv("INTERRUPT_REDIRECT", "true")

    from cli.commands.chat import _conversation_loop

    convo = MagicMock()
    convo.respond = AsyncMock(side_effect=[KeyboardInterrupt, "done"])

    reads: list = []

    async def _tracking_reader():
        if not reads:
            reads.append("original task")
            return "original task"
        elif len(reads) == 1:
            reads.append("my redirect")
            return "my redirect"
        else:
            raise EOFError

    await _conversation_loop(
        convo,
        MagicMock(),
        read_line=_tracking_reader,
    )

    # read_line was called: once for the original task, once for the redirect prompt.
    assert len(reads) >= 2
    assert reads[0] == "original task"
    assert reads[1] == "my redirect"
    # The redirect was sent to respond().
    assert convo.respond.call_args_list[1].args[0] == "my redirect"


# ---------------------------------------------------------------------------
# At-prompt Ctrl-C handler untouched (line :71)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_at_prompt_ctrl_c_still_continues_regardless_of_flag(monkeypatch, capsys):
    """Ctrl-C AT THE PROMPT (before respond()) continues the loop for both flag states."""
    for flag_val in ("true", "false"):
        monkeypatch.setenv("INTERRUPT_REDIRECT", flag_val)

        from cli.commands.chat import _conversation_loop

        convo = MagicMock()
        convo.respond = AsyncMock(return_value="ok")

        call_count = {"n": 0}

        async def _ki_then_real():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise KeyboardInterrupt  # at-prompt Ctrl-C
            elif call_count["n"] == 2:
                return "real input"
            else:
                raise EOFError

        await _conversation_loop(
            convo,
            MagicMock(),
            read_line=_ki_then_real,
        )

        # The real input ran as a turn.
        convo.respond.assert_awaited_once_with("real input")
        convo.reset_mock()
        call_count["n"] = 0
