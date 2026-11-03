"""The ONE owner of the REPL's "default model" (098 CL-05).

"Default" lives in three stores: ``cli.json`` (the REPL's new-session default),
the home ``.env`` ``DEFAULT_PROVIDER``/``DEFAULT_MODEL`` pin (which would
otherwise override ``cli.json``), and the per-tenant ``model_pins`` default (the
chat seats). Every caller writes and clears them through these two functions,
in a fixed order, and gets one result per store — a failed write is reported,
never swallowed. ``/model`` calls :func:`persist_default` only AFTER the live
swap succeeded, so a model that cannot run never becomes the default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass
class DefaultWrite:
    results: List[Tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(ok for _s, ok, _e in self.results)

    def add(self, store: str, fn) -> None:
        try:
            res = fn()
            if isinstance(res, tuple) and len(res) == 2 and res[0] is False:
                self.results.append((store, False, str(res[1] or "failed")))
                return
            self.results.append((store, True, ""))
        except Exception as exc:  # noqa: BLE001 — reported per store
            self.results.append((store, False, f"{type(exc).__name__}: {exc}"))

    def line(self, verb: str = "saved") -> str:
        parts = [f"{s} ✓" if ok else f"{s} ✗ ({err[:120]})" for s, ok, err in self.results]
        return f"Default for new sessions {verb}: " + " · ".join(parts)


def _env_path() -> Path:
    from core.paths import polyrob_home
    return polyrob_home() / ".env"


def _cli_set(provider: str, model: str) -> None:
    from cli.config_store import load_cli_config, save_cli_config
    cfg = load_cli_config()
    cfg["default_provider"], cfg["default_model"] = provider, model
    save_cli_config(cfg)


def _cli_clear() -> None:
    from cli.config_store import load_cli_config, save_cli_config
    cfg = load_cli_config()
    cfg.pop("default_provider", None)
    cfg.pop("default_model", None)
    save_cli_config(cfg)


def _env_set(provider: str, model: str) -> None:
    from core.env_file import upsert_env_var
    path = _env_path()
    upsert_env_var(path, "DEFAULT_PROVIDER", provider, secure=True)
    upsert_env_var(path, "DEFAULT_MODEL", model, secure=True)


def _env_clear() -> None:
    from core.env_file import remove_env_var
    path = _env_path()
    remove_env_var(path, "DEFAULT_PROVIDER", secure=True)
    remove_env_var(path, "DEFAULT_MODEL", secure=True)


def persist_default(provider: str, model: str, reasoning: Optional[str] = None, *,
                    tenant: Optional[str] = None) -> DefaultWrite:
    """Write the default: ``cli.json``, then ``.env``, then (with a tenant) the
    chat seats' ``model_pins`` default."""
    out = DefaultWrite()
    out.add("cli.json", lambda: _cli_set(provider, model))
    out.add(".env", lambda: _env_set(provider, model))
    if tenant:
        from core import model_pins
        out.add("chats", lambda: model_pins.set_default(tenant, provider, model, reasoning))
    return out


def clear_default(*, tenant: Optional[str] = None) -> DefaultWrite:
    """Remove the default from every store (``/model reset default``)."""
    out = DefaultWrite()
    out.add("cli.json", _cli_clear)
    out.add(".env", _env_clear)
    if tenant:
        from core import model_pins
        out.add("chats", lambda: model_pins.clear_default(tenant))
    return out
