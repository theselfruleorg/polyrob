"""Shell result shaping (073 W3): ONE helper for what the model reads back.

- ANSI escapes stripped (colour codes are noise in a transcript);
- high-confidence secret shapes redacted (``core.secret_scrub``) BEFORE the text
  enters the transcript or memory — a host `cat .env` must not land verbatim;
- over ``SHELL_MAX_OUTPUT_CHARS`` (default 50 000): keep 40 % head + 60 % tail around
  one notice, and save the FULL (redacted) text to the session ``logs/shell/`` so
  the model can read the middle with a file tool;
- a non-zero exit code gets one plain note (124 timeout, 137 SIGKILL/OOM, …).

No ``@BaseTool.action`` closures here — ``from __future__`` is safe.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable, Optional, Tuple, Union

_DEFAULT_MAX_CHARS = 50_000
_HEAD_SHARE = 0.4

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[@-Z\\-_]")

_EXIT_NOTES = {
    124: "timed out (killed by the time limit)",
    126: "command found but not executable",
    127: "command not found",
    130: "interrupted (SIGINT)",
    134: "aborted (SIGABRT)",
    137: "killed (SIGKILL — often out of memory or the time limit)",
    139: "crashed (SIGSEGV)",
    143: "terminated (SIGTERM)",
}


def max_output_chars() -> int:
    """``SHELL_MAX_OUTPUT_CHARS`` (default 50 000; floor 1 000; garbage -> default)."""
    raw = (os.getenv("SHELL_MAX_OUTPUT_CHARS") or "").strip()
    if not raw:
        return _DEFAULT_MAX_CHARS
    try:
        return max(1_000, int(raw))
    except ValueError:
        return _DEFAULT_MAX_CHARS


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def exit_note(rc: Optional[int]) -> str:
    if rc is None or rc == 0:
        return ""
    return _EXIT_NOTES.get(rc, "")


def _save_full(text: str, save_dir) -> Optional[str]:
    if callable(save_dir):
        try:
            save_dir = save_dir()
        except Exception:
            return None
    if save_dir is None:
        return None
    try:
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"shell-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{time.monotonic_ns() % 100000}.log"
        path.write_text(text, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return str(path)
    except Exception:
        return None


def shape_output(text: str, *, max_chars: Optional[int] = None,
                 save_dir: Union[Path, Callable[[], Optional[Path]], None] = None,
                 ) -> Tuple[str, Optional[str]]:
    """Return ``(text_for_the_model, full_output_path_or_None)``.

    ``save_dir`` may be a callable: it is resolved only when the text is actually
    truncated, so a short result never touches the filesystem."""
    from core.secret_scrub import scrub_secret_shapes
    clean = scrub_secret_shapes(strip_ansi(text or ""))
    cap = max_chars or max_output_chars()
    if len(clean) <= cap:
        return clean, None
    full_path = _save_full(clean, save_dir)
    head_n = int(cap * _HEAD_SHARE)
    tail_n = cap - head_n
    omitted = len(clean) - head_n - tail_n
    where = f" Full output: {full_path}" if full_path else ""
    notice = f"\n\n[... {omitted} characters omitted.{where} ...]\n\n"
    return clean[:head_n] + notice + clean[-tail_n:], full_path


__all__ = ["shape_output", "strip_ansi", "exit_note", "max_output_chars"]
