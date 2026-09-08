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
    canonicalize_provider,
    initializable_providers_with_keys,
    providers_with_keys,  # noqa: F401  (re-exported for callers)
    usable_providers_with_credentials,
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
    # Spec ALIASES (`glm` → `zai`, `kimi` → `moonshot`) canonicalize here — the LLM
    # manager registers clients under spec names only, so an un-canonicalized alias
    # dies downstream as "No client found for provider glm" even with a valid key.
    # Unknown names pass through as typed and error honestly at the manager.
    explicit_provider = canonicalize_provider(explicit_provider)
    pinned_provider = canonicalize_provider(pinned_provider)
    if cli_store_default:
        cli_store_default = (
            canonicalize_provider(cli_store_default[0]), cli_store_default[1],
        )
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
        present = usable_providers_with_credentials(env)

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


def operator_provider_pin(env=None):
    """The operator's serving-provider pin, if any (``CHAT_PROVIDER`` > ``DEFAULT_PROVIDER``).

    Returns the provider name or ``None``. Autonomous dispatch (goal/cron/planner) has no
    interactive caller to pass an explicit provider, so it must read this pin and thread it
    into ``resolve_runtime_config(..., pinned_provider=...)`` — otherwise the resolver falls
    to the "first keyed provider in canonical order" path, which picks a *canonical-first*
    provider even when the operator explicitly pinned a different (e.g. funded) one. This is
    the same pin ``task_agent_lite._resolve_chat_runtime`` honors for interactive runs.
    """
    env = _os.environ if env is None else env
    return (env.get("CHAT_PROVIDER") or env.get("DEFAULT_PROVIDER") or "").strip() or None


def resolve_default_provider(env=None, *, last_resort=("openai", None)):
    """Resolve the operator's default (provider, model) for an autonomous run.

    Thin wrapper over ``resolve_runtime_config`` that threads the operator pin
    (``CHAT_PROVIDER``/``DEFAULT_PROVIDER``) so autonomous dispatch honors it. ``model``
    may be ``None`` (the caller fills it from the registry via ``get_default_model``).
    """
    env = _os.environ if env is None else env
    return resolve_runtime_config(
        None, None, env=env,
        pinned_provider=operator_provider_pin(env),
        last_resort=last_resort,
    )


def resolve_session_runtime(provider=None, model=None, env=None):
    """Fill missing task-session provider/model from the operator's runtime
    config instead of a hardcoded openai/gpt-5 literal.

    Precedence (same ladder goals/cron dispatch and chat_once use): explicit arg
    > operator pin (``CHAT_PROVIDER``/``DEFAULT_PROVIDER``, model via
    ``DEFAULT_MODEL``) > first keyed provider in canonical order > openai/gpt-5
    last resort. The model is always filled to match the resolved provider, so a
    pinned/keyed provider never inherits the gpt-5 literal (live-prod
    2026-08-14: telegram sessions on a zai-coding-pinned box requested keyless
    openai and every inbound turn died once OpenRouter credits ran out).

    Returns ``(provider, model)`` where ``model`` may be None — the caller fills
    it from the model registry (that read deliberately stays OUT of core, same
    contract as ``resolve_runtime_config``). Fail-open: any resolver error
    returns the historical provider literal — session creation must never die
    on a config problem.
    """
    env = _os.environ if env is None else env
    try:
        # CHAT_ before DEFAULT_, mirroring operator_provider_pin's order — the
        # pin pair is read as a unit, so a CHAT_PROVIDER/CHAT_MODEL operator is
        # not silently served the registry default for their pinned provider.
        pinned_model = (
            env.get("CHAT_MODEL") or env.get("DEFAULT_MODEL") or ""
        ).strip() or None
        resolved_provider, resolved_model = resolve_runtime_config(
            provider or None,
            model or None,
            env=env,
            pinned_provider=operator_provider_pin(env),
            pinned_model=pinned_model,
            last_resort=("openai", None),
        )
        # An explicit model always survives (resolve_runtime_config's pin branch
        # returns the pinned model, not the caller's).
        return resolved_provider, (model or resolved_model)
    except Exception:
        return provider or "openai", model


def get_data_root() -> str:
    """Resolve the CLI/local runtime data home (where goals.db/cron.db/memory.db live).

    Delegates to the ONE data-home rule, ``core.runtime_paths.resolve_data_home``
    (``POLYROB_DATA_DIR`` wins, else ``cwd/.polyrob``); ``core.bootstrap.
    _resolve_cli_data_home`` resolves through the same seam, so the terminal-native
    ``polyrob goals``/``cron`` surfaces read the SAME database the autonomy dispatcher
    writes (``<data_root>/goals.db``). ``POLYROB_PROJECT_DIR`` moves only the
    workspace, never the data home (parity-pinned by
    tests/unit/core/test_data_home_resolver.py).
    """
    from core.runtime_paths import resolve_data_home
    return str(resolve_data_home())


def _sentinel_active(provider=None) -> bool:
    """Thin seam over the credit sentinel so tests can drive liveness directly."""
    try:
        from core.credit_sentinel import credit_sentinel_active
        return bool(credit_sentinel_active(provider))
    except Exception:
        return False


def resolve_live_provider(preferred=None, env=None):
    """The provider that can actually serve right now, preferring *preferred*.

    A pin says "prefer this". It must not also say "and never run again if this
    dies". Prod's 3-hourly digest job froze ``provider="zai-coding"`` into its
    payload on 2026-07-19; when that account hit its weekly cap on 2026-08-17
    every tick logged ``provider-credit sentinel active for zai-coding — $0
    skip`` and the job simply stopped, although a second credentialed provider
    was configured the whole time.

    Order: the preference when it is both credentialed and not credit-dead, then
    the first credentialed provider in canonical order that is not credit-dead.
    Returns ``None`` when nothing can serve — the honest signal for a caller to
    skip a paid tick, and the ONLY case that should skip one.
    """
    preferred = canonicalize_provider(preferred) if preferred else None
    try:
        candidates = list(usable_providers_with_credentials(env))
    except Exception:
        candidates = []
    if preferred and preferred in candidates and not _sentinel_active(preferred):
        return preferred
    for name in candidates:
        if not _sentinel_active(name):
            return name
    # Nothing in the candidate list is alive. An EMPTY list is not the same fact:
    # the credential oracle simply has no opinion (a test/dev env, an unreadable
    # store), and treating "I know of no providers" as "every provider is dead"
    # would skip every tick on a box that works fine. Keep the caller's
    # preference in that case and let the run fail honestly if it is really dead.
    if not candidates and preferred and not _sentinel_active(preferred):
        return preferred
    return None
