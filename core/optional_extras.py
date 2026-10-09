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
    # 073 W7: the cloud sandbox packs (SHELL_BACKEND=modal|daytona|vercel_sandbox).
    "modal": "modal",
    "daytona": "daytona",
    "vercel": "vercel-sandbox",
    # 058: the lean base. Each of these left [project].dependencies for the
    # extra that owns it; core/lazy_deps.py installs it on first use locally.
    "google": "gemini",            # google.generativeai (+ google.api_core, google.ai)
    "anthropic": "anthropic",
    "apsw": "memory-vector",
    "sqlite_vec": "memory-vector",
    "numpy": "media",              # also in [memory-vector]; H-MEM names that one itself
    "pypdf": "docs",
    "docx": "docs",
    "imageio": "media",
    "qrcode": "media",
    "magic": "server",             # python-magic — the upload MIME sniffer
    "solders": "solana",           # the Solana rail (solders + solana-py + x402[svm])
    "solana": "solana",
    "lark_oapi": "feishu",         # the Feishu / Lark WS long connection (surfaces/feishu/ws.py)
    "psutil": "browser",           # the orphaned-browser-process reaper
    "huggingface_hub": "hf",       # the HF Spaces deploy broker (tools/hf_deploy)
}

# extra name -> modules a preflight should probe for it
MODULES_FOR_EXTRA: dict[str, tuple[str, ...]] = {
    "browser": ("playwright",),
    "server": ("fastapi", "uvicorn", "magic"),
    "memory-vector": ("sentence_transformers", "apsw", "sqlite_vec", "numpy"),
    "crypto": ("web3", "eth_account"),
    "telegram": ("aiogram",),
    "twitter": ("tweepy",),
    "voice": ("faster_whisper",),
    "gemini": ("google.generativeai",),
    "anthropic": ("anthropic",),
    "docs": ("pypdf", "docx"),
    "media": ("numpy", "imageio", "qrcode"),
    "solana": ("solders", "solana"),
    "hf": ("huggingface_hub",),
    # 073 W7: the cloud sandbox packs (SHELL_BACKEND=<name>).
    "modal": ("modal",),
    "daytona": ("daytona",),
    "vercel-sandbox": ("vercel",),
    # anysite is a console SCRIPT, not an import — the discovery pack's
    # polyrob_discovery/anysite/client.py::binary_path
}
# A surface extra whose name IS its surface id is added outside the literal:
# the surface-catalog ratchet reads a literal holding two surface ids
# ("telegram" above) as a hand-kept surface list.
MODULES_FOR_EXTRA["feishu"] = ("lark_oapi",)   # surfaces/feishu/ws.py


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


def _spec_present(module: str) -> bool:
    """``find_spec`` that answers False instead of raising: for a DOTTED probe
    (``google.generativeai``) it raises ModuleNotFoundError when the parent
    package itself is absent — exactly the bare-install case being probed."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def extra_available(extra: str, modules: Optional[Iterable[str]] = None) -> bool:
    """True when every probe module for ``extra`` is importable."""
    probes = tuple(modules) if modules is not None else MODULES_FOR_EXTRA.get(extra, ())
    return all(_spec_present(m) for m in probes)


def require_extra(extra: str, modules: Optional[Iterable[str]] = None) -> None:
    """Raise ImportError with the canonical remedy when ``extra`` is absent.

    CLI commands call this BEFORE any side-effecting startup (container build,
    DB creation, opening browser tabs) so a missing extra fails in one line.
    """
    probes = tuple(modules) if modules is not None else MODULES_FOR_EXTRA.get(extra, ())
    missing = [m for m in probes if not _spec_present(m)]
    if missing:
        raise ImportError(
            f"this command needs the [{extra}] extra "
            f"(missing: {', '.join(missing)}) — {pip_hint(extra)}"
        )
