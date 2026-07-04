"""Single provider/model resolver consumed by BOTH the CLI and the server (Seam 2).

Historically the resolver lived in ``cli/config_store.py`` yet was imported by three
server modules (``api/openai_compat/model_map``, ``agents/task/goals/dispatcher``,
``cron/runner``) — a layering inversion, and a latent bug: that resolver read the
user's ``~/.polyrob/cli.json`` even on the server. This is the core home; CLI-only
persistence (``~/.polyrob/cli.json``) is INJECTED via ``cli_store_default`` (the server
passes ``None``), so the server never touches a home-dir file.

Precedence ladder:
    explicit arg (--provider / SessionRequest.provider)
      > pinned (CHAT_PROVIDER/DEFAULT_PROVIDER, caller-supplied)
      > cli_store_default (~/.polyrob/cli.json, CLI only)   [intersected]
      > first provider with a key, canonical PROFILES order  [intersected]
      > last_resort

"Intersected" = a provider with no key present is skipped. This applies ONLY to
``cli_store_default`` and the first-key fallback — NEVER to an explicit or pinned
provider (a user who types ``-p anthropic`` gets anthropic + a clean auth error).
"""
import os as _os

from modules.llm.profiles import (
    PROFILES,
    initializable_providers_with_keys,
    providers_with_keys,  # noqa: F401  (env_keys_present / callers may still use it)
    usable_providers_with_keys,
)


def resolve_runtime_config(
    explicit_provider,
    explicit_model,
    *,
    env=None,
    pinned_provider=None,
    pinned_model=None,
    cli_store_default=None,
    available_keys=None,
    last_resort=("openai", None),
):
    """Resolve (provider, model). ``model`` may be None (caller fills from registry)."""
    env = _os.environ if env is None else env
    # Canonical preference order, from the one Seam-1 oracle — restricted to
    # *initializable* providers so auto-select never emits a provider the LLM
    # manager can't bootstrap (e.g. deepseek → hard-crash). An explicit --provider
    # or operator pin (below) stays exempt and reaches the manager to error honestly.
    if available_keys is not None:
        # Name-based presence: callers pass a set of env-var NAMES (values already
        # vetted upstream), so only the provider/initializable filter applies here.
        present = initializable_providers_with_keys({k: "1" for k in available_keys})
    else:
        # Real values available → apply full usability (rejects malformed/too-short).
        present = usable_providers_with_keys(env)

    # 1. Explicit arg — exempt from intersection. Don't leak a stored model of a
    #    different provider; the caller fills from the registry.
    if explicit_provider:
        return explicit_provider, explicit_model

    # 2. Operator pin (CHAT_/DEFAULT_ env, resolved by the caller) — exempt.
    if pinned_provider:
        return pinned_provider, pinned_model

    # 3. CLI stored default (~/.polyrob/cli.json) — honored only if it has a key.
    if cli_store_default:
        stored_provider, stored_model = cli_store_default
        if stored_provider and stored_provider in present:
            return stored_provider, stored_model

    # 4. First provider with a key, in canonical order.
    if present:
        return present[0], None

    # 5. Last resort.
    return last_resort


def env_keys_present(env=None) -> set:
    """Set of provider API-key env-var NAMES present in *env* (for available_keys)."""
    env = _os.environ if env is None else env
    return {p.env_key for p in PROFILES.values() if env.get(p.env_key)}


def get_data_root() -> str:
    """Resolve the CLI/local runtime data home (where goals.db/cron.db/memory.db live).

    Mirrors ``core.bootstrap._resolve_cli_data_home`` — the SSOT for the data-home
    rule: honors ``POLYROB_DATA_DIR``/``POLYROB_PROJECT_DIR``, else ``cwd/.polyrob``.
    The terminal-native ``polyrob goals``/``cron`` surfaces use this so they read the
    SAME database the autonomy dispatcher writes (``<data_root>/goals.db``). Lazy
    import avoids a module cycle with core.bootstrap.
    """
    from core.bootstrap import _resolve_cli_data_home
    data_home, _ws_is_project_root, _project_root = _resolve_cli_data_home()
    return str(data_home)
