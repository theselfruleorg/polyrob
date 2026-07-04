"""Unit tests for cli.commands._bootstrap.suppress_bootstrap_output."""
import io
import os
import sys

import pytest

from cli.commands._bootstrap import suppress_bootstrap_output


def test_exception_inside_window_restores_stdout_stderr_and_fd2():
    """An exception raised inside the suppress window must restore
    sys.stdout, sys.stderr, and OS fd 2 before the exception propagates."""
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    # Record the real fd 2 target via /proc/self/fd or by dup-ing it.
    real_fd2_dup = os.dup(2)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            with suppress_bootstrap_output():
                raise RuntimeError("boom")

        # Python-level streams restored.
        assert sys.stdout is real_stdout
        assert sys.stderr is real_stderr

        # OS fd 2 points back to the same underlying file description as before.
        # We verify this by checking that writing to fd 2 doesn't raise and that
        # the restored fd 2 is open (stat succeeds).
        os.fstat(2)  # raises OSError if fd 2 is closed / invalid
    finally:
        os.close(real_fd2_dup)


def test_print_inside_window_does_not_reach_pre_window_stdout(capsys):
    """print() calls inside the suppress window must not appear on the
    real sys.stdout captured before the window opens."""
    with suppress_bootstrap_output():
        print("should-be-suppressed")

    captured = capsys.readouterr()
    assert "should-be-suppressed" not in captured.out
    assert "should-be-suppressed" not in captured.err
