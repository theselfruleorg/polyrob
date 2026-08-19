"""Declarative provider profiles (roadmap P8, Reference §28; proposal 024 L0).

A ``ProviderProfile`` *describes* a provider — identity, auth, base URL, default
model, capability flags — and owns no client construction, credential rotation, or
streaming. Since proposal 024 the profile table is a *derivation* of the
``ProviderSpec`` registry (``modules/llm/provider_spec.py``), which also carries
user-declared providers from ``~/.polyrob/providers.yaml``. The legacy literal
table is kept as the ``LLM_PROVIDER_REGISTRY=off`` kill-switch path (byte-identical;
remove with the kill-switch after one release).

The default model deliberately reads from ``llm_client_registry.DEFAULT_MODELS`` so
the default-model *policy* stays single-sourced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional

from modules.llm.llm_client_registry import get_default_model


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    display_name: str
    env_key: str                      # API-key environment variable ("" = none)
    auth_type: str = "api_key"        # api_key | oauth_* | borrowed | none
    base_url: Optional[str] = None    # None => provider SDK default
    supports_native_tools: bool = True
    supports_vision: bool = True
    signup_url: Optional[str] = None
    initializable: bool = True        # False = key alone can't bootstrap a client
                                      # (deepseek: direct client disabled → use OpenRouter)
    subscription: bool = False        # flat-rate plan — per-token cost is meaningless
    prompt_in_init: bool = True       # `polyrob init` asks for this key by default
    #: Extra key vars this provider answers to, tried after ``env_key``. See
    #: ProviderSpec.env_key_aliases — a provider that publishes several names
    #: must not report "no key" to a user who used one of the others.
    env_key_aliases: tuple = ()

    def env_key_chain(self) -> list:
        """Every key var this provider answers to, primary first."""
        seen, out = set(), []
        for name in (self.env_key, *self.env_key_aliases):
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def key_present_in(self, env) -> bool:
        """True when ANY var in the chain carries a non-blank value."""
        return any(env.get(n) for n in self.env_key_chain())

    def key_value_in(self, env):
        """The value of the first var in the chain that is set, else None."""
        for name in self.env_key_chain():
            value = env.get(name)
            if value:
                return value
        return None

    @property
    def default_model(self) -> str:
        """The default model for this provider (single-sourced from the registry)."""
        return get_default_model(self.name)


# NOTE: insertion order IS the canonical preference order for "first provider with
# a key" (see ``providers_with_keys`` / ``core.runtime_config``). OpenRouter is FIRST
# (2026-06-24): it is the preferred default client whenever its key is present —
# explicit ``-p`` and operator pins (DEFAULT_PROVIDER/CHAT_PROVIDER) still win.
#
# Kill-switch path (LLM_PROVIDER_REGISTRY=off) ONLY — the live table is derived
# from provider_spec.get_specs(). Keep in lockstep with BUILTIN_SPECS (pinned by
# the characterization suite in both modes) until the kill-switch is removed.
_LEGACY_PROFILES: Dict[str, ProviderProfile] = {
    "openrouter": ProviderProfile(
        name="openrouter", display_name="OpenRouter", env_key="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1", supports_native_tools=True,
        supports_vision=True, signup_url="https://openrouter.ai/",
    ),
    "anthropic": ProviderProfile(
        name="anthropic", display_name="Anthropic", env_key="ANTHROPIC_API_KEY",
        base_url="https://api.anthropic.com", supports_native_tools=True,
        supports_vision=True, signup_url="https://console.anthropic.com/",
    ),
    "openai": ProviderProfile(
        name="openai", display_name="OpenAI", env_key="OPENAI_API_KEY",
        supports_native_tools=True, supports_vision=True,
        signup_url="https://platform.openai.com/",
    ),
    "gemini": ProviderProfile(
        name="gemini", display_name="Google Gemini", env_key="GEMINI_API_KEY",
        supports_native_tools=True, supports_vision=True,
        signup_url="https://aistudio.google.com/",
    ),
    "nvidia": ProviderProfile(
        name="nvidia", display_name="NVIDIA NIM", env_key="NVIDIA_API_KEY",
        base_url="https://integrate.api.nvidia.com/v1", supports_native_tools=True,
        supports_vision=False, signup_url="https://build.nvidia.com/",
    ),
    "deepseek": ProviderProfile(
        name="deepseek", display_name="DeepSeek", env_key="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/v1", supports_native_tools=False,
        supports_vision=False, signup_url="https://platform.deepseek.com/",
        # Direct client disabled (tool-calling broken) — reach DeepSeek via OpenRouter
        # (OPENROUTER_API_KEY + model deepseek/deepseek-chat). A DEEPSEEK_API_KEY alone
        # cannot bootstrap the agent, so it must NOT count toward "has a usable key".
        initializable=False,
    ),
}


def _profiles_from_specs() -> Dict[str, ProviderProfile]:
    """Derive the profile view from the ProviderSpec registry (proposal 024)."""
    from modules.llm.provider_spec import get_specs
    out: Dict[str, ProviderProfile] = {}
    for s in get_specs():
        out[s.name] = ProviderProfile(
            name=s.name,
            display_name=s.display_name,
            env_key=s.env_key or "",
            auth_type=s.auth_type.value,
            base_url=s.resolved_base_url(),
            supports_native_tools=s.supports_native_tools,
            supports_vision=s.supports_vision,
            signup_url=s.signup_url,
            initializable=s.initializable,
            subscription=s.subscription,
            prompt_in_init=s.prompt_in_init,
            env_key_aliases=tuple(s.env_key_aliases),
        )
    return out


class _LazyProfiles:
    """Read-only dict-like view over the provider registry.

    Built on first access (keeps module import light: no YAML/file I/O at import,
    per tests/test_import_layering.py) and cached; ``reset()`` clears the snapshot
    (called by ``provider_spec.reset_provider_registry_cache``).
    """

    def __init__(self) -> None:
        self._snapshot: Optional[Dict[str, ProviderProfile]] = None

    def _ensure(self) -> Dict[str, ProviderProfile]:
        if self._snapshot is None:
            from modules.llm.provider_spec import provider_registry_enabled
            if provider_registry_enabled():
                self._snapshot = _profiles_from_specs()
            else:
                self._snapshot = dict(_LEGACY_PROFILES)
        return self._snapshot

    def reset(self) -> None:
        self._snapshot = None

    # read-only mapping interface (order-preserving)
    def __getitem__(self, key: str) -> ProviderProfile:
        return self._ensure()[key]

    def __contains__(self, key: object) -> bool:
        return key in self._ensure()

    def __iter__(self) -> Iterator[str]:
        return iter(self._ensure())

    def __len__(self) -> int:
        return len(self._ensure())

    def __bool__(self) -> bool:
        return bool(self._ensure())

    def get(self, key: str, default=None):
        return self._ensure().get(key, default)

    def keys(self):
        return self._ensure().keys()

    def values(self):
        return self._ensure().values()

    def items(self):
        return self._ensure().items()


PROFILES = _LazyProfiles()


def canonicalize_provider(name):
    """Canonical spec NAME for a provider name-or-alias; unknown/empty → unchanged.

    Re-export seam for ``core`` consumers (the layering ratchet allows
    ``core → modules.llm.profiles`` only) — logic lives in
    ``provider_spec.canonicalize_provider``.
    """
    from modules.llm.provider_spec import canonicalize_provider as _canon
    return _canon(name)


def providers_with_keys(env=None) -> List[str]:
    """Return provider names whose API key is present in *env*, in PROFILES order.

    This is the single source of truth for "which LLM providers have a key" — it
    replaces the divergent per-call-site key lists across the CLI (config_store,
    chat.py, doctor.py, model.py, banner.py). The returned order IS the canonical
    preference order for "first provider with a key".

    *env* defaults to ``os.environ``; pass a mapping for testability. A blank value
    counts as absent.
    """
    import os
    env = os.environ if env is None else env
    return [p.name for p in PROFILES.values() if p.key_present_in(env)]


def _keyless_by_design(p: ProviderProfile) -> bool:
    """True for a provider that needs no credential at all (``auth_type: none``,
    e.g. a local Ollama declared in providers.yaml). Such providers count as
    usable/initializable with no env key — the client sends a sentinel instead
    (``provider_spec.NO_KEY_SENTINEL``). Without this, a keyless box with a
    working local endpoint was refused by every no-key gate (B2, 2026-08-07).
    """
    return p.initializable and not p.env_key and p.auth_type == "none"


def initializable_providers_with_keys(env=None) -> List[str]:
    """``providers_with_keys`` restricted to providers a key ALONE can bootstrap.

    This is the SSOT for "does the user have a *usable* provider key" — the gating
    oracle that ``should_warn_no_key`` / the runtime resolver / the env-backfill and
    ``LLMManager._initialize``'s ``clients_to_try`` all derive from, so they can never
    disagree. Excludes deepseek (direct client disabled — route via OpenRouter).
    Includes keyless-by-design (``auth_type: none``) providers unconditionally. The
    raw ``providers_with_keys`` stays the DISPLAY oracle (doctor/webview show a key is
    present even when it can't bootstrap directly).
    """
    import os
    env = os.environ if env is None else env
    return [
        p.name for p in PROFILES.values()
        if _keyless_by_design(p)
        or (p.key_present_in(env) and p.initializable)
    ]


# Placeholder values that are "present" but not a real key (mirrors
# core.config.BotConfig.validate_api_keys, generalized across providers).
_PLACEHOLDER_KEYS = {
    "your-openai-key", "your-anthropic-key", "your-api-key", "your-alchemy-key",
    "your-gemini-key", "your-openrouter-key", "your-key", "changeme", "none", "null",
}
# Real provider API keys are comfortably longer than this; BotConfig blanks shorter
# ones for openai/anthropic. Every provider's real key (gemini ~39 is the shortest)
# clears 20, so the bar is safe to apply across providers.
_MIN_KEY_LEN = 20


def looks_like_real_key(value) -> bool:
    """True when *value* is a plausibly-real API key (not blank / placeholder / stub).

    Mirrors ``BotConfig.validate_api_keys`` so the GATING oracles agree with what the
    LLM manager will actually accept — a malformed key must NOT pass a guard only to be
    blanked by BotConfig and crash the manager with a misleading 'No API key found'.
    """
    if not value:
        return False
    v = str(value).strip()
    if v.lower() in _PLACEHOLDER_KEYS:
        return False
    return len(v) >= _MIN_KEY_LEN


def usable_providers_with_keys(env=None) -> List[str]:
    """Initializable providers whose key VALUE is well-formed (mirrors BotConfig).

    THE gating oracle wherever real key values are available (``should_warn_no_key``,
    the env-backfill, config-store resolution). Stricter than
    ``initializable_providers_with_keys`` (which is presence-only, for the resolver's
    name-based path) — it also rejects placeholder / too-short values. Keyless-by-
    design (``auth_type: none``) providers are always usable. Raw
    ``providers_with_keys`` remains the DISPLAY oracle.
    """
    import os
    env = os.environ if env is None else env
    return [
        p.name for p in PROFILES.values()
        if _keyless_by_design(p)
        or (p.initializable and looks_like_real_key(p.key_value_in(env)))
    ]


# ---------------------------------------------------------------------------
# Credential-aware oracles (proposal 024, L1.5 — "surface un-blinding")
# ---------------------------------------------------------------------------
# The three `*_with_keys` oracles above can only see ENV KEYS. Once credentials
# can also come from the auth store (an OAuth subscription seat, a borrowed
# login), every surface that asks "do we have a provider?" through them is
# blind to those — L1 was invisible without this.
#
# These three delegate to the ONE resolution oracle
# (`core.llm_auth.resolve.resolve_credential`) and add what it does not know:
# `initializable` (a key alone can't bootstrap deepseek) and the raw
# presence/validity split the display surfaces render.
#
# With `LLM_AUTH_STORE_ENABLED` off — the default — resolve_credential serves
# only its env-key and keyless rungs, so these return exactly what the
# `*_with_keys` oracles do. That equivalence is pinned by tests, which is what
# makes migrating a call site to these names safe.
#
# The ProviderProfile is passed straight through as the spec: resolve_credential
# duck-types `env_key`/`auth_type`, and a profile has both. That keeps this
# working with `LLM_PROVIDER_REGISTRY` off, where there are no ProviderSpecs.

@dataclass(frozen=True)
class CredentialStatus:
    """What one provider's credential IS — for display and for gating.

    `present` and `usable` are deliberately separate: a malformed key IS
    configured (say so, don't pretend it's absent) but cannot serve.
    """
    provider: str
    display_name: str
    present: bool                     # something is configured (DISPLAY)
    usable: bool                      # ...and it can actually serve (GATING)
    source: str                       # env | oauth | borrowed | none
    health: str = "ok"                # ok | rate_limited | exhausted
    expires_at: Optional[float] = None
    relogin_required: bool = False
    reason: str = ""                  # why not usable, when it isn't

    def __repr__(self) -> str:        # a status object must never carry a value
        return (
            f"CredentialStatus(provider={self.provider!r}, present={self.present}, "
            f"usable={self.usable}, source={self.source!r}, health={self.health!r})"
        )


#: 024 gates that must be read from the PROCESS env even when the caller passed
#: its own mapping. `resolve_credential(env=)` uses one mapping for both key
#: VALUES and these deployment FLAGS, but callers pass key-shaped dicts
#: (`doctor_report(env)`, `resolve_runtime_config(env=…)`, tests) — a caller
#: supplying `{"OPENAI_API_KEY": …}` is describing keys, NOT declaring the auth
#: store disabled. Reading the flags from that mapping silently pinned every
#: store rung off.
_DEPLOYMENT_FLAG_KEYS = ("LLM_AUTH_STORE_ENABLED", "LLM_CREDENTIAL_BORROW")


def _scoped_env(env) -> dict:
    """The caller's key mapping, with the deployment flags layered in.

    Only the flags are added — no provider key from the process leaks into a
    caller's mapping, so `credential_status({})` still means "no keys".
    """
    import os
    scoped = dict(env)
    for flag in _DEPLOYMENT_FLAG_KEYS:
        if flag not in scoped and flag in os.environ:
            scoped[flag] = os.environ[flag]
    return scoped


class _StoreSnapshot:
    """In-memory view of the auth store for ONE credential_status() sweep.

    ``AuthStore.get_provider`` re-reads the file under an flock on every call.
    With 30+ providers in the table that turned a single readiness check into
    30+ locked reads — and three oracles plus every gate call it. This reads the
    file once and answers the whole sweep from that snapshot.

    Snapshot semantics are correct here: a sweep reports one consistent
    point-in-time view, which is what a status listing should show anyway.
    """

    __slots__ = ("_providers", "_borrowed")

    def __init__(self, providers, borrowed):
        self._providers = providers or {}
        self._borrowed = borrowed or {}

    def get_provider(self, name):
        return self._providers.get(name)

    def get_borrowed(self, name):
        return self._borrowed.get(name)


def _profile_like(spec):
    """A ProviderSpec is already duck-compatible with resolve_credential
    (``env_key_chain`` + ``auth_type``); named for the reader."""
    return spec


def _shared_store(env):
    """A one-read snapshot of the auth store for a whole sweep, or None.

    None whenever the store rungs are off/refused — resolve_credential then
    takes only its env-key path and never touches disk at all.
    """
    try:
        from core.llm_auth.resolve import _store_rungs_allowed
        if not _store_rungs_allowed(env):
            return None
        from core.llm_auth.store import get_auth_store
        data = get_auth_store().load()          # ONE read for the whole sweep
        return _StoreSnapshot(data.get("providers"), data.get("borrowed"))
    except Exception:
        return None


def _resolve_one(profile: ProviderProfile, env, user_id, store=None):
    """resolve_credential for one profile; None on any error (fail-open).

    Fail-open direction is deliberate: a broken credential layer degrades the
    surfaces to "can't confirm this is usable", never to a crash and never to a
    false ready.
    """
    try:
        from core.llm_auth.resolve import resolve_credential
        return resolve_credential(
            profile.name, profile, env=env,
            key_validator=looks_like_real_key, user_id=user_id, store=store,
        )
    except Exception:
        return None


def credential_status(env=None, *, user_id=None) -> Dict[str, CredentialStatus]:
    """Per-provider credential state, in PROFILES order.

    THE display oracle for every surface that shows provider readiness
    (`doctor`, `model list`, the banner, the webview console card). `user_id`
    is forwarded to the tenancy gate — pass it wherever the requesting tenant is
    known, so a non-owner is never shown (or served) the owner's seat.
    """
    import os
    env = os.environ if env is None else env
    resolve_env = _scoped_env(env)
    # Resolve the store ONCE and hand the same handle to every provider. Each
    # `get_provider` is a file read under an flock, and this loop now runs over
    # 30+ providers — re-resolving per provider turned one readiness check into
    # dozens of locked reads, and three oracles call this.
    store = _shared_store(resolve_env)
    out: Dict[str, CredentialStatus] = {}
    for p in PROFILES.values():
        cred = _resolve_one(p, resolve_env, user_id, store)
        raw_present = p.key_present_in(env)
        keyless = _keyless_by_design(p)
        present = raw_present or keyless or cred is not None
        source = cred.source if cred is not None else ("env" if raw_present else "none")
        health = cred.health if cred is not None else "ok"
        relogin = bool(cred is not None and cred.relogin_required)

        reason = ""
        if not present:
            reason = f"no credential (set {p.env_key})" if p.env_key else "no credential"
        elif cred is None:
            # Configured but the resolver refused it: a placeholder/too-short
            # value is the overwhelmingly common cause, and saying "missing"
            # there sends people hunting for a key they already pasted.
            reason = "malformed (placeholder or too short to be a real key)"
        elif not p.initializable:
            reason = "direct client disabled — route via OpenRouter"
        elif relogin:
            reason = "credential expired — reconnect required"
        elif health != "ok":
            reason = f"credential {health}"

        out[p.name] = CredentialStatus(
            provider=p.name,
            display_name=p.display_name,
            present=present,
            usable=(cred is not None and p.initializable and health == "ok" and not relogin),
            source=source,
            health=health,
            expires_at=cred.expires_at if cred is not None else None,
            relogin_required=relogin,
            reason=reason,
        )
    return out


def providers_with_credentials(env=None, *, user_id=None) -> List[str]:
    """DISPLAY oracle: providers with SOME credential, healthy or not.

    The credential-aware counterpart of ``providers_with_keys`` — same job
    (show the user what is configured), but it also sees store-backed
    credentials. Includes non-initializable providers and malformed values,
    exactly as ``providers_with_keys`` does.
    """
    return [n for n, s in credential_status(env, user_id=user_id).items() if s.present]


def usable_providers_with_credentials(env=None, *, user_id=None) -> List[str]:
    """GATING oracle: providers that can actually serve a request right now.

    The credential-aware counterpart of ``usable_providers_with_keys``. Adds two
    exclusions the key-only oracle cannot make: an EXPIRED store credential
    (reconnect required) and an unhealthy one (rate-limited / quota exhausted) —
    for a flat-rate seat, exhaustion is the normal daily condition, and hammering
    a spent plan is worse than routing around it.
    """
    return [n for n, s in credential_status(env, user_id=user_id).items() if s.usable]


def unusable_but_present(env=None, *, user_id=None) -> List[CredentialStatus]:
    """Configured providers that cannot serve — so a "no provider" message can
    say WHY (expired / rate-limited / malformed) instead of "no key found"."""
    return [s for s in credential_status(env, user_id=user_id).values()
            if s.present and not s.usable]


def no_key_message() -> str:
    """The single canonical no-key message (neutral module — no ``cli`` import).

    Re-exported from ``cli.keys`` and reused by ``LLMManager._initialize``'s raise so
    every no-key surface says the same thing. The provider list derives from the
    spec registry (NOT a literal), so a user-declared providers.yaml row's env key
    is named too — the message must never deny the user's own configuration.
    """
    # Name only the providers `polyrob init` actually onboards. Listing every
    # variable in the table was 27 names and ~950 characters of wall — the
    # opposite of helpful on a first run. The rest are one command away.
    # Anything the USER declared in providers.yaml is always named, even when it
    # overrides a built-in that init doesn't prompt for: they wrote that row, so
    # its variable is exactly the guidance they need.
    try:
        from modules.llm.provider_spec import user_providers_report
        declared = set(user_providers_report().get("loaded") or ())
    except Exception:
        declared = set()

    parts = []
    extra = 0
    for p in PROFILES.values():
        if not p.env_key:
            continue  # keyless-by-design rows have no key to set
        if not p.prompt_in_init and p.name not in declared:
            extra += 1
            continue
        label = p.env_key
        if p.name == "openrouter":
            label += " (recommended)"
        if not p.initializable:
            label += (
                " (direct client disabled — use OPENROUTER_API_KEY with model "
                "deepseek/deepseek-chat)"
            )
        parts.append(label)
    more = (f" ({extra} more providers — subscription plans, gateways, regional "
            f"endpoints: `polyrob model list`)" if extra else "")
    # 027 WP4: ONE remedy grammar on every no-key surface — `polyrob auth add`
    # first, `polyrob init` for the guided path. The old six-file env-ladder
    # wall belongs in the docs, not a first-run error.
    return (
        "No usable provider credential found.\n"
        "  connect one:   polyrob auth add <provider>   (openrouter recommended — "
        "one key, every model)\n"
        "  guided setup:  polyrob init\n"
        "Keys live in ~/.polyrob/.env (or process env). Providers: "
        + ", ".join(parts) + "." + more + "\n"
        "Keyless/custom endpoints (a local Ollama, vLLM, a corporate gateway) need no "
        "key — declare them in ~/.polyrob/providers.yaml."
    )


def get_profile(name: str) -> Optional[ProviderProfile]:
    return PROFILES.get(name)


def all_profiles() -> List[ProviderProfile]:
    return list(PROFILES.values())


def extra_llm_config_blocks(env=None) -> Dict[str, Dict[str, object]]:
    """Per-provider LLM-config additions from the ProviderSpec registry (024 seam 2).

    Consumed by ``core.config.BotConfig.get_llm_config()`` through the existing,
    layering-allowlisted ``core → modules.llm.profiles`` edge. Returns:

    - a full ``{"api_key", "base_url", "auth_type"}`` block for every provider with
      no hand-written literal block in ``get_llm_config`` — a user-declared
      providers.yaml row, OR a built-in served by the generic transport clients
      (the 024 T0 subscription rows) — so ``create_llm_client``/``LLMManager`` can
      bootstrap it exactly like a legacy built-in; and
    - a ``{"base_url": ...}`` override for a LEGACY built-in provider whose effective
      base URL was redirected via providers.yaml / its ``base_url_env`` — merged into
      the literal block so clients that read config (OpenAIClient) see it.

    Empty (byte-identical config) when the registry is off or no user file exists.
    Fail-open: any registry error yields ``{}`` — config building must never break.
    """
    import os
    env = os.environ if env is None else env
    try:
        from modules.llm.provider_spec import (
            BUILTIN_SPECS,
            get_specs,
            needs_synthetic_config_block,
            provider_registry_enabled,
        )
        if not provider_registry_enabled():
            return {}
        out: Dict[str, Dict[str, object]] = {}
        builtin_defaults = {b.name: b for b in BUILTIN_SPECS}
        for s in get_specs():
            if s.builtin and not needs_synthetic_config_block(s):
                default = builtin_defaults.get(s.name)
                if default is None:
                    continue
                resolved = s.resolved_base_url(env)
                if resolved != default.base_url:
                    out.setdefault(s.name, {})["base_url"] = resolved
                # An env_key override on a built-in (corporate gateway with its
                # own key var) must also re-source the api_key — the BotConfig
                # pydantic field keeps reading the ORIGINAL var otherwise.
                if s.env_key and s.env_key != default.env_key:
                    _n, override_key = s.resolve_env_key(env)
                    if override_key:
                        out.setdefault(s.name, {})["api_key"] = override_key
            else:
                _name, key = s.resolve_env_key(env)
                if not key:
                    # No env key — try the CREDENTIAL oracle (an OAuth seat, a
                    # borrowed login). Without this the whole L1/L2 store is
                    # decorative for inference: `polyrob auth add` succeeds,
                    # doctor reports the provider ready, and every call dies
                    # with "API key not provided" because the client layer only
                    # ever saw environment variables.
                    cred = _resolve_one(_profile_like(s), _scoped_env(env), None,
                                        _shared_store(_scoped_env(env)))
                    if cred is not None and cred.value:
                        key = cred.value
                if not key and s.auth_type.value == "none":
                    # AuthType.NONE (Ollama & friends): the manager's bootstrap
                    # gates skip providers with no api_key — the sentinel makes
                    # a keyless local endpoint bootstrappable (024 live-path fix).
                    from modules.llm.provider_spec import NO_KEY_SENTINEL
                    key = NO_KEY_SENTINEL
                out[s.name] = {
                    "api_key": key,
                    "base_url": s.resolved_base_url(env),
                    "auth_type": s.auth_type.value,
                }
        return out
    except Exception:
        return {}
