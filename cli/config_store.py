"""Persistent CLI preferences (roadmap P3).

A tiny JSON store so `rob model set-default` survives across invocations without
touching the server/env config. The path is overridable via ``POLYROB_CLI_CONFIG``
(used for test isolation); otherwise it defaults to ``~/.polyrob/cli.json``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _config_path() -> Path:
    override = os.getenv("POLYROB_CLI_CONFIG")
    if override:
        return Path(override)
    from core.paths import polyrob_home
    return polyrob_home() / "cli.json"


def load_cli_config() -> Dict[str, Any]:
    """Return the stored CLI config, or {} if missing/corrupt (never raises).

    On corrupt JSON the file is *renamed aside* to ``<path>.bak.<timestamp>``
    (rename-aside so a subsequent :func:`save_cli_config` writes a clean file
    without having to overwrite the corrupt bytes), a WARNING is logged naming
    the backup path, and ``{}`` is returned.  If the rename itself fails (e.g.
    read-only filesystem) the error is logged and we still return ``{}`` — the
    function never raises.
    """
    p = _config_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        # --- corrupt-config recovery ---
        # Only attempt backup when there is content worth preserving.
        try:
            raw = p.read_bytes()
        except OSError:
            raw = b""
        if raw:
            ts = int(time.time())
            bak = p.parent / f"{p.name}.bak.{ts}"
            try:
                p.rename(bak)
                logger.warning(
                    "cli.json was corrupt; backed up to %s and falling back to defaults",
                    bak,
                )
            except OSError as exc:
                logger.warning(
                    "cli.json was corrupt and backup to .bak failed (%s); falling back to defaults",
                    exc,
                )
        return {}


def save_cli_config(data: Dict[str, Any]) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True))


def set_default_model(provider: str, model: str) -> None:
    cfg = load_cli_config()
    cfg["default_provider"] = provider
    cfg["default_model"] = model
    save_cli_config(cfg)


def get_default_model() -> Tuple[Optional[str], Optional[str]]:
    """Return (provider, model) defaults, each None if unset."""
    cfg = load_cli_config()
    return cfg.get("default_provider"), cfg.get("default_model")


def env_default_override_note(saved_provider: str) -> Optional[str]:
    """Return a note if an env operator-pin will OVERRIDE the just-saved cli.json
    default for new sessions, else None.

    ``/model`` and ``polyrob model set-default`` persist to ``~/.polyrob/cli.json``
    (precedence 3), but a ``CHAT_PROVIDER``/``DEFAULT_PROVIDER`` env pin (precedence
    2, e.g. written by ``polyrob init``) wins for new sessions — so the "saved as
    default" message would be misleading. Detect that by asking the shared resolver
    what a fresh session would actually pick and comparing the provider. Fail-open.
    """
    try:
        resolved_provider, resolved_model = resolve_provider_model(None, None)
    except Exception:
        return None
    if resolved_provider and resolved_provider != saved_provider:
        tail = f"/{resolved_model}" if resolved_model else ""
        return (
            f"Note: an environment default ({resolved_provider}{tail}) takes precedence "
            f"for new sessions. Unset CHAT_/DEFAULT_PROVIDER (e.g. `polyrob config unset "
            f"DEFAULT_PROVIDER`) to make this the default."
        )
    return None


def _provider_for_model(model: Optional[str]) -> Optional[str]:
    """Return the provider that owns *model* in the registry, or None.

    No model string appears under more than one provider's ``AVAILABLE_MODELS``, so
    the lookup is unambiguous. Used to pair an explicit ``--model`` with the RIGHT
    provider when ``--provider`` is omitted (e.g. ``-m gpt-5`` must be openai, not
    whatever auto-resolved). Returns None for unknown/custom models (keep the
    resolution as-is).
    """
    if not model:
        return None
    from modules.llm.llm_client_registry import AVAILABLE_MODELS
    for provider, models in AVAILABLE_MODELS.items():
        if model in models:
            return provider
    return None


def resolve_model_alias(name: Optional[str]) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """Expand a ``model_aliases`` entry from the CLI config to ``(provider, model)``.

    Hermes-style QoL alias, e.g. ``model_aliases: {"fav": "anthropic/claude-sonnet-4-5"}``
    in ``cli.json`` so ``/model fav`` and ``-m fav`` shortcut a full provider/model pair.
    The stored value may be:
      - a ``"provider/model"`` slug
      - a ``{"provider": ..., "model": ...}`` dict
      - a bare model name (provider is ``None``; the caller infers it, e.g. via
        ``_provider_for_model``)
    Returns ``None`` when *name* isn't a defined alias (never raises — callers treat
    ``None`` as "not an alias, try the normal path").
    """
    cfg = load_cli_config() or {}
    aliases = cfg.get("model_aliases") or {}
    val = aliases.get((name or "").strip())
    if not val:
        return None
    if isinstance(val, dict):
        return val.get("provider"), val.get("model")
    if isinstance(val, str) and "/" in val:
        provider, model = val.split("/", 1)
        return provider, model
    if isinstance(val, str):
        return None, val
    return None


def check_provider_model(provider: str, model: str) -> Tuple[bool, str]:
    """Validate a (provider, model) pair for a set-default persist.

    Returns ``(provider_known, warning)``. ``provider_known=False`` => reject (the
    provider isn't a real profile). ``warning`` is a non-empty note when the model
    isn't a known model for a known provider — persisting is still allowed (the
    registry may lag a freshly-released model), the caller just surfaces the note.
    Shared by ``model set-default`` (CLI) and ``/model`` (REPL) so they can't drift.
    """
    from modules.llm.profiles import get_profile
    from modules.llm.llm_client_registry import AVAILABLE_MODELS
    if get_profile(provider) is None:
        return False, ""
    known = AVAILABLE_MODELS.get(provider, [])
    if known and model not in known:
        return True, f"'{model}' isn't a known {provider} model; persisting anyway."
    return True, ""


# NOTE: deepseek is intentionally OMITTED — its direct client is disabled, so a
# DEEPSEEK_API_KEY must never AUTO-resolve as the provider (that would hand the LLM
# manager a provider it can't build → hard crash). An explicit `-p deepseek` flows
# through `explicit_provider`, not `available_keys`, so it is unaffected. Reach
# DeepSeek via OPENROUTER_API_KEY + model deepseek/deepseek-chat.
_KEY_TO_PROVIDER = [
    ("ANTHROPIC_API_KEY", "anthropic"),
    ("OPENAI_API_KEY", "openai"),
    ("GEMINI_API_KEY", "gemini"),
    ("OPENROUTER_API_KEY", "openrouter"),
    ("NVIDIA_API_KEY", "nvidia"),
]


def resolve_provider_model(cli_provider, cli_model, *, available_keys=None):
    """CLI provider/model resolution — thin wrapper over the core resolver.

    Injects the CLI-only persistence (``~/.polyrob/cli.json`` via ``get_default_model``)
    and the legacy ``gemini`` last resort, then delegates to
    ``core.runtime_config.resolve_runtime_config`` (the SSOT both surfaces share).
    Returns (provider, model); model may be None (caller fills from registry).
    """
    from core.runtime_config import resolve_runtime_config

    if available_keys is None:
        # Only count a key as "available" if its VALUE is well-formed (mirrors
        # BotConfig / the manager) — a malformed/too-short key must not resolve a
        # provider the manager will then reject.
        from modules.llm.profiles import looks_like_real_key
        available_keys = {
            k for k, _ in _KEY_TO_PROVIDER if looks_like_real_key(os.environ.get(k))
        }
    # `model_aliases` (B6): a bare `-m fav` expands to its (provider, model) pair
    # BEFORE the rest of the resolution runs, so the pin/store/registry logic below
    # sees a normal explicit model. An explicit `--provider` still wins over the
    # alias's own provider (the user typed it on purpose); a provider-less alias
    # value falls through to `_provider_for_model` inference below as usual.
    if cli_model:
        _alias = resolve_model_alias(cli_model)
        if _alias:
            _alias_provider, _alias_model = _alias
            if _alias_model:
                cli_provider = cli_provider or _alias_provider
                cli_model = _alias_model
    # Operator pin (precedence 2, below an explicit --provider/--model): CHAT_* wins
    # over DEFAULT_*. This is where `polyrob init` persists its chosen default (to
    # ~/.polyrob/.env), so run/chat/doctor must read it here — the same env the
    # chat_once path reads (task_agent_lite). Without this the wizard's choice is dead.
    pinned_provider = os.environ.get("CHAT_PROVIDER") or os.environ.get("DEFAULT_PROVIDER")
    pinned_model = os.environ.get("CHAT_MODEL") or os.environ.get("DEFAULT_MODEL")
    provider, model = resolve_runtime_config(
        cli_provider,
        cli_model,
        available_keys=available_keys,
        pinned_provider=pinned_provider,
        pinned_model=pinned_model,
        cli_store_default=get_default_model(),
        last_resort=("gemini", None),
    )
    # Preserve an explicit ``--model`` even when ``--provider`` is absent: it must
    # win over a stored/registry model for the resolved provider. AND, when no
    # ``--provider`` was given, pair it with the model's TRUE owner (e.g. ``-m gpt-5``
    # → openai, not whatever auto-resolved) so we don't hand the manager an incoherent
    # provider/model pair. Unknown/custom models keep the resolved provider.
    if cli_model:
        model = cli_model
        if not cli_provider:
            inferred = _provider_for_model(cli_model)
            if inferred:
                provider = inferred
    return provider, model


def migrate_to_dotenv() -> None:
    """One-time: fold ~/.polyrob/cli.json into ~/.polyrob/.env.

    ``default_provider``/``default_model`` move to env vars (that's what env is for).
    ``model_aliases`` has no env equivalent (it's a name->pair map), so when present
    the json is REWRITTEN to contain only ``{"model_aliases": ...}`` instead of being
    deleted — otherwise a user's aliases would silently vanish the first time this
    migration ran. With no aliases the file is deleted as before.
    """
    cfg = load_cli_config()
    if not cfg:
        return
    env_path = _config_path().parent / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    have = {ln.split("=", 1)[0] for ln in lines if "=" in ln}
    if cfg.get("default_provider") and "DEFAULT_PROVIDER" not in have:
        lines.append(f"DEFAULT_PROVIDER={cfg['default_provider']}")
    if cfg.get("default_model") and "DEFAULT_MODEL" not in have:
        lines.append(f"DEFAULT_MODEL={cfg['default_model']}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n")
    aliases = cfg.get("model_aliases")
    if aliases:
        save_cli_config({"model_aliases": aliases})
    else:
        try:
            _config_path().unlink()
        except OSError:
            pass
