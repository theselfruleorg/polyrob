"""Profile-local history without credential-bearing input."""
import logging
import os
import re

from prompt_toolkit.history import FileHistory

#: ``/config set <KEY> …`` — the KEY decides, through the ONE secret-name
#: predicate (``core.secrets.is_secret_key``), not a local marker list (CLI3).
_CONFIG_SET_RE = re.compile(r"^\s*/config\s+set\s+(\S+)", re.IGNORECASE)

#: A bare 64-hex run, with or without ``0x``: an EVM private key has exactly the
#: shape of a transaction hash, so the display scrubber has to let it through
#: (CLI15). History has no such duty — a line that carries one is omitted.
_HEX64_RE = re.compile(r"(?<![0-9A-Fa-f])(?:0x)?[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")


def _secret_bearing(string: str) -> bool:
    from cli.ui.secrets import scrub_secrets
    from core.secrets import is_secret_key
    upper = string.upper()
    if scrub_secrets(string) != string or any(
        marker in upper for marker in ("API_KEY", "SECRET", "PASSWORD", "MNEMONIC", "/MCP ADD", "/AUTH ")
    ):
        return True
    m = _CONFIG_SET_RE.match(string)
    if m and is_secret_key(m.group(1)):
        return True
    return bool(_HEX64_RE.search(string))


class TerminalHistory(FileHistory):
    def store_string(self, string):
        # Omit rather than redact commands: replaying a redacted mutation is
        # misleading. Credential setup has no reason to enter arrow-key history.
        if _secret_bearing(string):
            return
        try:
            # Create it 0600 so the first line is never world-readable.
            os.close(os.open(self.filename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
            super().store_string(string)
        except OSError as exc:
            if not getattr(self, "_write_warning", False):
                logging.getLogger(__name__).warning("Input history could not be saved: %s", exc)
                self._write_warning = True
            return
        # CLI3: the file is the owner's alone (0600), created or inherited.
        try:
            os.chmod(self.filename, 0o600)
        except OSError:
            pass
