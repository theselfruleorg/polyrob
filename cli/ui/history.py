"""Profile-local history without credential-bearing input."""
from prompt_toolkit.history import FileHistory
import logging


class TerminalHistory(FileHistory):
    def store_string(self, string):
        from cli.ui.secrets import scrub_secrets
        # Omit rather than redact commands: replaying a redacted mutation is
        # misleading. Credential setup has no reason to enter arrow-key history.
        upper = string.upper()
        if scrub_secrets(string) != string or any(
            marker in upper for marker in ("API_KEY", "SECRET", "PASSWORD", "MNEMONIC", "/MCP ADD", "/AUTH ")
        ):
            return
        try:
            super().store_string(string)
        except OSError as exc:
            if not getattr(self, "_write_warning", False):
                logging.getLogger(__name__).warning("Input history could not be saved: %s", exc)
                self._write_warning = True
