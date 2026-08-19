"""Honest exits + actionable failure rendering for one-shot runs (027 WP3).

`polyrob run` exited 0 on a halted session (bad key -> FATAL -> rc=0), and a
first-task 401 spilled two raw tracebacks without ever naming the fix."""

import logging
import sys

from cli.commands._errors import remedy_line, session_exit_code
from cli.ui.log_squelch import single_line_error_formatter


class TestSessionExitCode:
    def test_failed_session_done_is_exit_1(self):
        assert session_exit_code(False, "whatever") == 1

    def test_successful_session_done_is_exit_0(self):
        assert session_exit_code(True, "all good") == 0

    def test_sentinel_failure_strings_are_exit_1_without_session_done(self):
        for text in (
            "Session failed: FATAL ERROR: no fallback",
            "Task package not available: No module named 'x'",
            "No active session found",
            "Session not found or unauthorized",
            "Session cancelled by user",
        ):
            assert session_exit_code(None, text) == 1, text

    def test_normal_answer_without_session_done_is_exit_0(self):
        assert session_exit_code(None, "Here is your summary.") == 0


class TestRemedyLine:
    def test_auth_error_names_auth_add(self):
        line = remedy_line(
            "No fallback available after LLMAuthenticationError from openrouter: 401"
        )
        assert "polyrob auth add openrouter" in line

    def test_auth_error_without_provider_still_actionable(self):
        line = remedy_line("Error code: 401 - Unauthorized")
        assert "polyrob auth add" in line

    def test_missing_module_maps_to_extra(self):
        line = remedy_line("No module named 'playwright'")
        assert "polyrob[browser]" in line

    def test_credit_death_names_billing(self):
        line = remedy_line("insufficient_quota: you exceeded your current quota")
        assert "billing" in line.lower() or "credit" in line.lower()

    def test_unknown_error_returns_none(self):
        assert remedy_line("something exploded uniquely") is None


class TestRequireExtraOrExit:
    def test_missing_extra_exits_1_with_remedy(self, capsys):
        import pytest

        from cli.commands._errors import require_extra_or_exit

        with pytest.raises(SystemExit) as exc:
            require_extra_or_exit("telegram", modules=("definitely_not_a_dep",))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "pip install 'polyrob[telegram]'" in err

    def test_present_extra_is_quiet(self, capsys):
        from cli.commands._errors import require_extra_or_exit

        require_extra_or_exit("server", modules=("os",))
        assert capsys.readouterr().err == ""


class TestLogSquelch:
    def test_formatter_drops_traceback_keeps_message(self):
        formatter = single_line_error_formatter()
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                "x", logging.ERROR, __file__, 1, "it broke", (), sys.exc_info()
            )
        rendered = formatter.format(record)
        assert "it broke" in rendered
        assert "Traceback" not in rendered
        assert "ValueError" not in rendered
