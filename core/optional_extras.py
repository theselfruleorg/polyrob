"""One map from a missing optional dependency to its pip extra (proposal 027).

POLYROB ships optional features as pip extras (``polyrob[browser]``,
``polyrob[server]``, ...). When one is absent, every surface — CLI preflight,
TaskAgent init, tool registration — must name the SAME remedy instead of
leaking a raw ``ModuleNotFoundError``. This module is that single source.
"""

from __future__ import annotations

import importlib.util
import re
from typing import Iterable, Optional

# import-name (top-level module) -> extra name in pyproject.toml
EXTRA_FOR_MODULE: dict[str, str] = {
    "playwright": "browser",
    "fastapi": "server",
    "starlette": "server",
    "uvicorn": "server",
    "socketio": "server",
    "multipart": "server",
    "watchfiles": "server",
    "argon2": "server",
    "jinja2": "server",
    "sentence_transformers": "memory-vector",
    "web3": "crypto",
    "eth_account": "crypto",
    "x402": "crypto",
    "hyperliquid": "crypto",
    "aiogram": "telegram",
    "tweepy": "twitter",
    "faster_whisper": "voice",
}

# extra name -> modules a preflight should probe for it
MODULES_FOR_EXTRA: dict[str, tuple[str, ...]] = {
    "browser": ("playwright",),
    "server": ("fastapi", "uvicorn"),
    "memory-vector": ("sentence_transformers",),
    "crypto": ("web3",),
    "telegram": ("aiogram",),
    "twitter": ("tweepy",),
    "voice": ("faster_whisper",),
}


def pip_hint(extra: str) -> str:
    """The canonical remedy string for a missing extra."""
    hint = f"pip install 'polyrob[{extra}]'"
    if extra == "browser":
        hint += " && python -m playwright install chromium"
    return hint


def extra_for_module(module: str) -> Optional[str]:
    """Map an import name (dotted ok) to its extra, or None if unknown."""
    return EXTRA_FOR_MODULE.get(module.split(".")[0])


def missing_extra_hint(error_text: str) -> Optional[str]:
    """Turn ``No module named 'x'`` text into a full remedy line, or None.

    Accepts a raw exception string so call sites can feed ``str(exc)`` from an
    ImportError they caught anywhere down an import chain.
    """
    match = re.search(r"No module named '?([A-Za-z0-9_\.]+)'?", error_text or "")
    if not match:
        return None
    extra = extra_for_module(match.group(1))
    if extra is None:
        return None
    return (
        f"missing optional dependency '{match.group(1)}' — install the "
        f"[{extra}] extra: {pip_hint(extra)}"
    )


def chromium_missing_hint(error_text: str) -> Optional[str]:
    """Remedy for playwright's installed-but-no-browser launch failure."""
    if "Executable doesn't exist" not in (error_text or ""):
        return None
    return (
        "playwright is installed but its browser is not — run: "
        "python -m playwright install chromium"
    )


def extra_available(extra: str, modules: Optional[Iterable[str]] = None) -> bool:
    """True when every probe module for ``extra`` is importable."""
    probes = tuple(modules) if modules is not None else MODULES_FOR_EXTRA.get(extra, ())
    return all(importlib.util.find_spec(m) is not None for m in probes)


def require_extra(extra: str, modules: Optional[Iterable[str]] = None) -> None:
    """Raise ImportError with the canonical remedy when ``extra`` is absent.

    CLI commands call this BEFORE any side-effecting startup (container build,
    DB creation, opening browser tabs) so a missing extra fails in one line.
    """
    probes = tuple(modules) if modules is not None else MODULES_FOR_EXTRA.get(extra, ())
    missing = [m for m in probes if importlib.util.find_spec(m) is None]
    if missing:
        raise ImportError(
            f"this command needs the [{extra}] extra "
            f"(missing: {', '.join(missing)}) — {pip_hint(extra)}"
        )
