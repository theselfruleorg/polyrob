"""Declarative LLM provider registry (proposal 024, L0).

One ``ProviderSpec`` row describes a provider completely — identity, credential
shape, transport, base URL, capabilities, derivation data — so the historical
per-provider literal lists (``PROFILES``, ``PROVIDER_CONFIG``, ``DEFAULT_MODELS``,
``SCHEMA_GENERATORS``, ``native_providers``, ``_KEY_TO_PROVIDER``,
``FALLBACK_HIERARCHY``, ``_KNOWN_PROVIDERS``, ``_PREFIX_TO_PROVIDER``) become
*derivations* of this table instead of hand-maintained copies. Same move
``core/tool_capabilities.py`` made for tools in WS-2.

User-declared providers load from ``~/.polyrob/providers.yaml`` (path override:
``LLM_CUSTOM_PROVIDERS``; set-but-empty disables). Loading is lazy + cached —
this module must stay light at import (entry-point weight test) and must never
pull agents.*/aiogram/provider SDKs (layering test on ``modules.llm.profiles``).

The ``LLM_PROVIDER_REGISTRY`` kill-switch (and the duplicate legacy literal tables
behind it) was removed 2026-08-29, two releases after it shipped in 0.10.0; every seam
derives from ``get_specs()`` and falls back to ``BUILTIN_SPECS`` (this module's own
data) on a registry fault, never to a second hand-maintained copy.

Security invariants (proposal 024 §4.0.3, §7.1):
- user YAML may NEVER name a ``client_class_name`` — it can only select an
  existing generic transport (otherwise a file on disk is a code-exec vector);
- ``providers.yaml`` is credential-equivalent (it redirects ``base_url``) and is
  denied to agent tools via ``core/security/secret_guard.py``;
- loading is fail-open PER ENTRY: a malformed row is skipped with a WARN, never
  a crash.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


class AuthType(str, Enum):
    API_KEY = "api_key"
    OAUTH_DEVICE = "oauth_device"
    OAUTH_PKCE = "oauth_pkce"
    BORROWED = "borrowed"
    NONE = "none"          # e.g. a local Ollama loopback — no credential needed


class Transport(str, Enum):
    CHAT_COMPLETIONS = "chat_completions"      # OpenAI-compatible /chat/completions
    ANTHROPIC_MESSAGES = "anthropic_messages"  # Anthropic /v1/messages shape
    RESPONSES = "responses"                    # OpenAI Responses API


@dataclass(frozen=True)
class OAuthSpec:
    """OAuth flow descriptor (consumed by L2; declared here so specs are complete).

    Real subscription providers deviate from vanilla RFC 8628 / RFC 7636 in
    small, incompatible ways. Rather than a bespoke flow per vendor, the
    deviations are DATA on this record — the four fields below are exactly the
    axes observed across the shipped rows:

    - ``device_auth_style`` — OpenAI's device-auth leg takes a JSON body, not
      the form encoding RFC 8628 specifies.
    - ``redirect_mode`` — Anthropic redirects to a hosted callback page that
      DISPLAYS the code for the user to paste back, rather than to a loopback
      port. That is also the only PKCE variant that works over SSH.
    - ``token_exchange_url`` — GitHub Copilot's OAuth token is not the
      inference credential; it must be exchanged for a short-lived Copilot JWT.
    - ``token_user_agent`` — some token endpoints gate on User-Agent.
    """
    auth_url: str = ""
    token_url: str = ""
    client_id: str = ""
    scopes: Tuple[str, ...] = ()
    grant: str = "device_code"
    refresh_skew_sec: int = 120
    #: "form" (RFC 8628) | "json" — how the device-authorization leg is encoded.
    device_auth_style: str = "form"
    #: "loopback" (127.0.0.1 listener) | "manual" (user pastes the code back).
    #: "manual" is the only PKCE mode usable on a headless box.
    redirect_mode: str = "loopback"
    #: Optional second leg: exchange the OAuth token for the real inference
    #: credential (Copilot). Empty = the OAuth access_token IS the credential.
    token_exchange_url: str = ""
    #: Optional fixed redirect_uri (a hosted callback page for redirect_mode
    #: "manual"). Empty = the flow mints a loopback URI.
    redirect_uri: str = ""
    #: Optional User-Agent for the TOKEN endpoint only.
    token_user_agent: str = ""
    #: The ``grant_type`` sent when redeeming a device code. Defaults to RFC
    #: 8628's urn; MiniMax uses a non-standard one, and sending the wrong urn is
    #: an unsupported_grant_type rejection at the very last step of the flow.
    token_grant_type: str = "urn:ietf:params:oauth:grant-type:device_code"


@dataclass(frozen=True)
class ProviderSpec:
    # identity
    name: str
    display_name: str
    aliases: Tuple[str, ...] = ()

    # credentials
    auth_type: AuthType = AuthType.API_KEY
    env_key: Optional[str] = None            # PRIMARY key var (None for OAUTH-only / NONE)
    #: Additional accepted key vars, tried in order AFTER ``env_key``. Real
    #: providers publish more than one name — z.ai answers to GLM_API_KEY,
    #: ZAI_API_KEY and Z_AI_API_KEY; Copilot to COPILOT_GITHUB_TOKEN, GH_TOKEN
    #: and GITHUB_TOKEN. Honouring only the primary means a user who followed
    #: the vendor's own docs gets told they have no key.
    #:
    #: ``env_key`` stays the one we NAME (in prompts, `no_key_message`, doctor)
    #: so guidance never becomes a list of six equivalent variables.
    env_key_aliases: Tuple[str, ...] = ()
    oauth: Optional[OAuthSpec] = None

    # transport
    transport: Transport = Transport.CHAT_COMPLETIONS
    base_url: Optional[str] = None           # None => SDK default
    base_url_env: Optional[str] = None       # operator override, e.g. OLLAMA_BASE_URL
    #: ``((key_prefix, base_url), ...)`` — a vendor that issues DIFFERENT key
    #: families for different endpoints (Moonshot: legacy platform keys speak
    #: api.moonshot.ai/v1, `sk-kimi-` Kimi Code keys only work against
    #: api.kimi.com/coding). Matching a prefix redirects the base URL, so a
    #: subscriber pasting their key does not get a 401 from the wrong host.
    #: ``base_url_env`` still wins — an explicit operator override is final.
    key_prefix_base_urls: Tuple[Tuple[str, str], ...] = ()
    extra_headers: Tuple[Tuple[str, str], ...] = ()
    bearer_auth: bool = False                # ANTHROPIC_MESSAGES only: send the key as
                                             # Authorization: Bearer (z.ai) not x-api-key

    # capabilities (feed the derivations)
    supports_native_tools: bool = True
    supports_vision: bool = True
    default_model: Optional[str] = None      # None => llm_client_registry.DEFAULT_MODELS
    models: Tuple[str, ...] = ()             # declared model list (§4.0.1)
    model_prefixes: Tuple[str, ...] = ()     # feeds openai-compat prefix routing
    fallback_eligible: bool = False
    fallback_rank: Optional[int] = None      # ordering within FALLBACK_HIERARCHY
    initializable: bool = True
    client_class_name: Optional[str] = None  # None => generic client for `transport`

    # provenance
    builtin: bool = False
    subscription: bool = False               # flat-rate plan, not metered
    signup_url: Optional[str] = None
    tos_note: Optional[str] = None           # surfaced at connect time (§7.4)
    prompt_in_init: bool = True              # ask for this key in `polyrob init`
    # How prompt caching is achieved on this provider (UP-08 / S4 2026-08-29):
    # "in_client" (the client marks the stable prefix itself), "automatic"
    # (server-side, no request change), "explicit" (Gemini cachedContents),
    # "breakpoints" (cache_control markers), "none". None => derive: an
    # OpenRouter route is model-dependent (cache_hints.openrouter_cache_strategy);
    # an ANTHROPIC_MESSAGES transport inherits the Anthropic client's in-client
    # breakpoints; anything else "none". Consumed by cache_hints.provider_cache_strategy.
    cache_strategy: Optional[str] = None

    def headers(self) -> Dict[str, str]:
        return dict(self.extra_headers)

    def env_key_chain(self) -> Tuple[str, ...]:
        """Every key var this provider answers to, primary first."""
        seen, out = set(), []
        for name in (self.env_key, *self.env_key_aliases):
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return tuple(out)

    def resolve_env_key(self, env=None) -> Tuple[Optional[str], Optional[str]]:
        """``(var_name, value)`` for the first key var in the chain that is set.

        Returns ``(None, None)`` when none is. The NAME comes back too so a
        caller can say which variable it actually used — with a chain, "a key is
        present" is not enough to debug with.
        """
        env = os.environ if env is None else env
        for name in self.env_key_chain():
            value = env.get(name)
            if value and str(value).strip():
                return name, value
        return None, None

    def resolved_base_url(self, env=None, api_key: Optional[str] = None) -> Optional[str]:
        """The effective base URL.

        Precedence: ``base_url_env`` (an explicit operator override is final) >
        a ``key_prefix_base_urls`` match on *api_key* > ``base_url``.
        """
        env = os.environ if env is None else env
        if self.base_url_env:
            override = env.get(self.base_url_env)
            if override and str(override).strip():
                return str(override).strip()
        if api_key and self.key_prefix_base_urls:
            for prefix, url in self.key_prefix_base_urls:
                if str(api_key).startswith(prefix):
                    return url
        return self.base_url


# ---------------------------------------------------------------------------
# Built-in specs — canonical order IS the "first provider with a key" order
# (mirrors the legacy modules/llm/profiles.py PROFILES table; fallback_rank
# mirrors the legacy LLMManager.FALLBACK_HIERARCHY order; model_prefixes mirror
# the pre-024 api/openai_compat/model_map prefix table).
# ---------------------------------------------------------------------------
BUILTIN_SPECS: Tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="openrouter", display_name="OpenRouter", env_key="OPENROUTER_API_KEY",
        transport=Transport.CHAT_COMPLETIONS, base_url="https://openrouter.ai/api/v1",
        supports_native_tools=True, supports_vision=True,
        signup_url="https://openrouter.ai/", client_class_name="OpenRouterClient",
        fallback_eligible=True, fallback_rank=2, builtin=True,
    ),
    ProviderSpec(
        name="anthropic", display_name="Anthropic", env_key="ANTHROPIC_API_KEY",
        transport=Transport.ANTHROPIC_MESSAGES, base_url="https://api.anthropic.com",
        supports_native_tools=True, supports_vision=True,
        signup_url="https://console.anthropic.com/", client_class_name="AnthropicClient",
        fallback_eligible=True, fallback_rank=1, model_prefixes=("claude",), builtin=True,
        cache_strategy="in_client",
    ),
    ProviderSpec(
        name="openai", display_name="OpenAI", env_key="OPENAI_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        supports_native_tools=True, supports_vision=True,
        signup_url="https://platform.openai.com/", client_class_name="OpenAIClient",
        cache_strategy="in_client",
        fallback_eligible=True, fallback_rank=0, model_prefixes=("gpt", "o1", "o3"),
        builtin=True,
    ),
    ProviderSpec(
        name="gemini", display_name="Google Gemini", env_key="GEMINI_API_KEY",
        # transport is INERT for builtins with a bespoke client_class_name (the
        # factory/schema paths match on name first) — gemini actually speaks the
        # google SDK, not chat_completions. Do not trust spec.transport for a
        # builtin unless its client_class_name is None.
        transport=Transport.CHAT_COMPLETIONS,
        supports_native_tools=True, supports_vision=True,
        signup_url="https://aistudio.google.com/", client_class_name="GeminiClient",
        fallback_eligible=True, fallback_rank=3, model_prefixes=("gemini",), builtin=True,
        cache_strategy="explicit",  # implicit is free + needs no code; explicit is opt-in
    ),
    ProviderSpec(
        name="nvidia", display_name="NVIDIA NIM", env_key="NVIDIA_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://integrate.api.nvidia.com/v1",
        supports_native_tools=True, supports_vision=False,
        signup_url="https://build.nvidia.com/", client_class_name="NvidiaClient",
        model_prefixes=("kimi",), builtin=True,
        cache_strategy="automatic",  # NIM KV reuse is operator-side
    ),
    ProviderSpec(
        name="deepseek", display_name="DeepSeek", env_key="DEEPSEEK_API_KEY",
        transport=Transport.CHAT_COMPLETIONS, base_url="https://api.deepseek.com/v1",
        supports_native_tools=False, supports_vision=False,
        signup_url="https://platform.deepseek.com/", client_class_name="DeepSeekClient",
        # Direct client disabled (tool-calling broken) — reach DeepSeek via OpenRouter.
        initializable=False, model_prefixes=("deepseek",), builtin=True,
        cache_strategy="automatic",  # disk cache, server-side
    ),

    # -----------------------------------------------------------------------
    # Subscription / flat-rate plans that issue an API KEY (024 T0, 2026-08-11)
    # -----------------------------------------------------------------------
    # These are the plans a user CAN connect today — no OAuth needed, the vendor
    # hands out a key. Before this they had to hand-author a providers.yaml row.
    #
    # Rules for a row in this block:
    #   * appended AFTER the six legacy built-ins, so the canonical
    #     "first provider with a key" preference order is unchanged for every
    #     existing install;
    #   * ``client_class_name=None`` — served by the generic transport clients
    #     (``compat_clients.py``) exactly like a providers.yaml row, so no new
    #     client code and no new BotConfig field is needed (the api_key block is
    #     synthesized by ``profiles.extra_llm_config_blocks``);
    #   * ``fallback_eligible=False`` — a subscription seat is the owner's, and
    #     silently failing over ONTO it (or off it) is never right;
    #   * NO ``model_prefixes`` — several of these serve the same open-weight
    #     model ids (glm/kimi/gpt-oss), so prefix routing would be ambiguous.
    #     Reach them with an explicit ``-p <provider>``;
    #   * ``supports_vision=False`` — conservative. None of these model ids are
    #     in the model registry, so the spec value is what the client falls back
    #     to; sending an image to a text-only endpoint is a hard error, while
    #     declining to is merely a lost capability. Override per row in
    #     providers.yaml once you have verified vision on your plan.
    #   * ``subscription=True`` ONLY when the ENDPOINT is exclusively served
    #     under a flat-rate plan (it zeroes per-token cost accounting — see
    #     ``is_flat_rate``). An endpoint that ALSO serves pay-as-you-go keeps
    #     the metered default.
    #
    # 024 §8 live-verification status (2026-08-14): `zai-coding` is
    # LIVE-VERIFIED — real key, full tool-calling agent runs on prod
    # (glm-4.6, VPS) and local (glm-5), AnthropicCompatClient over
    # api.z.ai/api/anthropic. ⚠ `cerebras` / `ollama-cloud` remain declared
    # from vendor documentation only (no key on any box); a wrong model
    # id/base URL surfaces as a provider 4xx, never a silent wrong answer.
    # See proposal 024 §8 and the 2026-08-11 subscription-auth status ledger.
    ProviderSpec(
        name="ollama-cloud", display_name="Ollama Cloud", aliases=("ollama_cloud",),
        env_key="OLLAMA_API_KEY",
        transport=Transport.CHAT_COMPLETIONS, base_url="https://ollama.com/v1",
        base_url_env="OLLAMA_CLOUD_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="glm-5.2",
        models=("glm-5.2", "kimi-k2.6", "deepseek-v4-flash", "minimax-m2.7",
                "gpt-oss:120b", "gpt-oss:20b"),
        fallback_eligible=False, builtin=True, subscription=True,
        # Opt-in plan, not part of the six-provider onboarding path — `polyrob
        # init` names it instead of adding a prompt nobody without the plan
        # can answer (the wizard's Section 1/6 is position-sensitive).
        prompt_in_init=False,
        signup_url="https://ollama.com/",
        # Distinct from a LOCAL Ollama (http://127.0.0.1:11434/v1, auth_type:
        # none) — that one stays a providers.yaml row named `ollama`. Same
        # vendor, different product: this one is hosted and needs a key.
        tos_note=(
            "Ollama Cloud is a flat-rate plan (Free/Pro/Max) billed on GPU time, "
            "not per token — usage here does not appear in per-token cost accounting."
        ),
    ),
    ProviderSpec(
        # No "zai" alias: that is now a real provider row (the standard
        # pay-as-you-go API on a DIFFERENT endpoint). An alias that shadows a
        # real provider name is a routing bug waiting to happen.
        name="zai-coding", display_name="z.ai GLM Coding Plan", aliases=("zai-code",),
        env_key="ZAI_API_KEY",
        transport=Transport.ANTHROPIC_MESSAGES, base_url="https://api.z.ai/api/anthropic",
        bearer_auth=True,   # z.ai authenticates Bearer, not x-api-key
        # supports_vision is the PROVIDER-wide floor, and it stays False because
        # this seat's flagship (glm-5.3) is text-only. Per-model truth lives in the
        # model registry, which `_check_vision_support` consults FIRST — so a
        # session pinned to glm-5.3-flash (the one multimodal model on this plan,
        # live-verified 2026-09-15) still gets vision.
        supports_native_tools=True, supports_vision=False,
        # Newest first. glm-5/glm-5.2 stay listed: a pinned older model must keep
        # resolving, and `polyrob doctor` renders this tuple as what the seat serves.
        default_model="glm-5.3",
        models=("glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5"),
        fallback_eligible=False, builtin=True, subscription=True,
        # Opt-in plan, not part of the six-provider onboarding path — `polyrob
        # init` names it instead of adding a prompt nobody without the plan
        # can answer (the wizard's Section 1/6 is position-sensitive).
        prompt_in_init=False,
        signup_url="https://z.ai/",
        tos_note=(
            "The z.ai /api/anthropic endpoint is the GLM Coding Plan seat (flat "
            "monthly/quarterly) — usage is not metered per token."
        ),
    ),
    ProviderSpec(
        name="cerebras", display_name="Cerebras", env_key="CEREBRAS_API_KEY",
        transport=Transport.CHAT_COMPLETIONS, base_url="https://api.cerebras.ai/v1",
        supports_native_tools=True, supports_vision=False,
        default_model="zai-glm-4.7",
        models=("zai-glm-4.7", "gpt-oss-120b", "qwen-3-coder-480b"),
        fallback_eligible=False, builtin=True,
        # NOT subscription=True: one endpoint + one key serves BOTH the flat-rate
        # Code Pro/Max plans and pay-per-token. Defaulting to metered can only
        # overstate cost; a Code subscriber sets `subscription: true` on a
        # `cerebras:` row in providers.yaml to zero it.
        subscription=False,
        # Opt-in plan, not part of the six-provider onboarding path — `polyrob
        # init` names it instead of adding a prompt nobody without the plan
        # can answer (the wizard's Section 1/6 is position-sensitive).
        prompt_in_init=False,
        signup_url="https://cloud.cerebras.ai/",
        tos_note=(
            "Cerebras Code Pro/Max are flat-rate plans on the same endpoint as "
            "pay-per-token. On a Code plan, set `subscription: true` for the "
            "`cerebras` row in providers.yaml so per-token accounting is skipped."
        ),
    ),
    # -----------------------------------------------------------------------
    # Extended provider breadth (2026-08-12)
    # -----------------------------------------------------------------------
    # Endpoints + env-var chains taken from each vendor's public docs; model
    # ids and tool-call capability verified against models.dev.
    # Every default_model below is a model that catalog marks
    # tool_call-capable — a default that cannot call tools is useless to an
    # agent and shows up as the silent tools=0 failure.
    #
    # Same rules as the T0 block: appended last, fallback-ineligible, no
    # model_prefixes, prompt_in_init=False, generic transport clients, and
    # supports_vision=False unless verified.
    #
    # NOT shipped, deliberately: arcee / gmi / actual (absent from models.dev —
    # no way to verify a model id, and a wrong one reads as our bug); LM Studio
    # and other loopback servers (a KEYLESS built-in would count as "usable" on
    # every box and permanently silence the no-provider warning — those stay
    # providers.yaml rows); azure-foundry / vertex / bedrock (no fixed endpoint
    # or non-key auth — separate work).
    ProviderSpec(
        name="zai", display_name="Z.AI (GLM)", aliases=("glm",),
        env_key="GLM_API_KEY", env_key_aliases=("Z_AI_API_KEY",),
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://api.z.ai/api/paas/v4", base_url_env="GLM_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        # The default stays glm-5.2 deliberately: this row is METERED, and moving it
        # to the pricier 5.3 would raise every existing user's bill without them
        # asking. The two 5.3 ids are listed because the endpoint serves them (live
        # /api/paas/v4/models, 2026-09-15) and because AUX_MODEL_MAP routes this
        # provider's aux work to glm-5.3-flash — a map row naming a model absent from
        # the seat's own tuple is exactly the drift this file exists to prevent.
        default_model="glm-5.2",
        models=("glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5",
                "glm-4.7-flash", "glm-4.6v"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
        signup_url="https://z.ai/",
        # Deliberately does NOT alias ZAI_API_KEY: that is the `zai-coding`
        # row's primary, and one key claimed by two rows with DIFFERENT
        # endpoints routes a coding-plan key at the standard API (401) or the
        # reverse. Two products, two variables.
        tos_note="Standard pay-as-you-go Z.AI API. The flat-rate Coding Plan is the `zai-coding` row.",
    ),
    ProviderSpec(
        name="moonshot", display_name="Moonshot AI (Kimi)", aliases=("kimi",),
        env_key="KIMI_API_KEY", env_key_aliases=("MOONSHOT_API_KEY",),
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://api.moonshot.ai/v1", base_url_env="KIMI_BASE_URL",
        # A Kimi Code (`sk-kimi-`) key is rejected by the legacy platform host;
        # it only works against the coding endpoint. Redirect on the prefix so a
        # subscriber who pastes their key does not get an unexplained 401.
        key_prefix_base_urls=(("sk-kimi-", "https://api.kimi.com/coding/v1"),),
        supports_native_tools=True, supports_vision=False,
        default_model="kimi-k2.6",
        models=("kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
        signup_url="https://platform.moonshot.ai/",
    ),
    ProviderSpec(
        name="moonshot-cn", display_name="Moonshot AI (China)",
        env_key="KIMI_CN_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://api.moonshot.cn/v1", base_url_env="KIMI_CN_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="kimi-k2.6",
        models=("kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
    ),
    ProviderSpec(
        name="kimi-coding", display_name="Kimi For Coding",
        env_key="KIMI_CODING_API_KEY",
        # models.dev serves this provider as @ai-sdk/anthropic — the Coding
        # Plan endpoint is Anthropic-shaped, NOT the OpenAI-compatible
        # api.moonshot.ai/v1 that the `moonshot` row above uses.
        transport=Transport.ANTHROPIC_MESSAGES,
        base_url="https://api.kimi.com/coding", base_url_env="KIMI_CODING_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="kimi-for-coding",
        models=("kimi-for-coding", "kimi-for-coding-highspeed", "k3", "k3-256k"),
        fallback_eligible=False, builtin=True, subscription=True, prompt_in_init=False,
        signup_url="https://kimi.com/code",
        tos_note="Kimi Code is a flat-rate plan billed on a refreshing quota, not per token.",
    ),
    ProviderSpec(
        name="minimax", display_name="MiniMax",
        env_key="MINIMAX_API_KEY",
        transport=Transport.ANTHROPIC_MESSAGES,
        base_url="https://api.minimax.io/anthropic", base_url_env="MINIMAX_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="MiniMax-M2.7",
        models=("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed",
                "MiniMax-M2.5", "MiniMax-M2.1"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
        signup_url="https://platform.minimax.io/",
    ),
    ProviderSpec(
        name="minimax-cn", display_name="MiniMax (China)",
        env_key="MINIMAX_CN_API_KEY",
        transport=Transport.ANTHROPIC_MESSAGES,
        base_url="https://api.minimaxi.com/anthropic", base_url_env="MINIMAX_CN_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="MiniMax-M2.7",
        models=("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
    ),
    ProviderSpec(
        name="xai", display_name="xAI (Grok)", aliases=("grok",),
        env_key="XAI_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://api.x.ai/v1", base_url_env="XAI_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="grok-4.3",
        models=("grok-4.5", "grok-4.3", "grok-build-0.1"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
        signup_url="https://console.x.ai/",
    ),
    ProviderSpec(
        name="dashscope", display_name="Qwen Cloud (DashScope)", aliases=("qwen",),
        env_key="DASHSCOPE_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        base_url_env="DASHSCOPE_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="qwen3-coder-plus",
        models=("qwen3.7-plus", "qwen3-coder-plus", "qwen3-coder-480b-a35b-instruct",
                "qwen3-coder-flash"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
        signup_url="https://dashscope.console.aliyun.com/",
    ),
    ProviderSpec(
        name="alibaba-coding", display_name="Alibaba Cloud Coding Plan",
        env_key="ALIBABA_CODING_PLAN_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        base_url_env="ALIBABA_CODING_PLAN_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="qwen3-coder-plus",
        models=("qwen3-coder-plus", "qwen3-coder-flash"),
        fallback_eligible=False, builtin=True, subscription=True, prompt_in_init=False,
        # Some agents also accept DASHSCOPE_API_KEY here. We don't: it is the
        # `dashscope` row's primary, and one variable claimed by two rows with
        # different endpoints mis-routes whichever loses the ordering.
        tos_note="Flat-rate coding plan on a separate endpoint from pay-as-you-go DashScope.",
    ),
    ProviderSpec(
        name="stepfun", display_name="StepFun (Step Plan)",
        env_key="STEPFUN_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://api.stepfun.ai/step_plan/v1", base_url_env="STEPFUN_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="step-3.5-flash",
        models=("step-3.7-flash", "step-3.5-flash"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
    ),
    ProviderSpec(
        name="ai-gateway", display_name="Vercel AI Gateway", aliases=("vercel",),
        env_key="AI_GATEWAY_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://ai-gateway.vercel.sh/v1", base_url_env="AI_GATEWAY_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="anthropic/claude-sonnet-4.6",
        models=("anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet-5",
                "openai/gpt-5", "openai/gpt-5-codex"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
        signup_url="https://vercel.com/ai-gateway",
    ),
    ProviderSpec(
        name="opencode-zen", display_name="OpenCode Zen",
        env_key="OPENCODE_ZEN_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://opencode.ai/zen/v1", base_url_env="OPENCODE_ZEN_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="claude-sonnet-4-6",
        models=("claude-sonnet-4-6", "claude-opus-5", "glm-5.2", "deepseek-v4-flash"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
    ),
    ProviderSpec(
        name="opencode-go", display_name="OpenCode Go",
        env_key="OPENCODE_GO_API_KEY",
        # ⚠ This gateway mixes wire shapes BY MODEL (GLM/Kimi speak
        # chat_completions; MiniMax and Qwen 3.7 speak Anthropic Messages). A
        # ProviderSpec carries ONE transport, so the declared models below are
        # the chat_completions half. Reach the Anthropic-shaped models through a
        # providers.yaml row with transport: anthropic_messages.
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://opencode.ai/zen/go/v1", base_url_env="OPENCODE_GO_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="glm-5",
        models=("glm-5", "kimi-k3", "deepseek-v4-flash"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
    ),
    ProviderSpec(
        name="kilocode", display_name="Kilo Gateway", aliases=("kilo",),
        env_key="KILOCODE_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://api.kilo.ai/api/gateway", base_url_env="KILOCODE_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="anthropic/claude-sonnet-4.6",
        models=("anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet-5", "openai/gpt-5"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
    ),
    ProviderSpec(
        name="huggingface", display_name="Hugging Face", aliases=("hf",),
        # NOT HF_TOKEN: `huggingface-cli login`, datasets and transformers all
        # set it, so it is present on a large share of dev machines for reasons
        # unrelated to inference. Claiming it would silently route ALL of a
        # box's inference through HF's router the moment it was the only
        # "credential" present. Same rule applied to GH_TOKEN for copilot.
        env_key="HUGGINGFACE_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://router.huggingface.co/v1", base_url_env="HF_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="zai-org/GLM-5.2",
        models=("zai-org/GLM-5.2", "moonshotai/Kimi-K3", "deepseek-ai/DeepSeek-V4-Flash-0731"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
        signup_url="https://huggingface.co/settings/tokens",
    ),
    ProviderSpec(
        name="xiaomi", display_name="Xiaomi MiMo",
        env_key="XIAOMI_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://api.xiaomimimo.com/v1", base_url_env="XIAOMI_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="mimo-v2.5-pro",
        models=("mimo-v2.5-pro", "mimo-v2.5", "mimo-v2-flash"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
    ),
    ProviderSpec(
        name="tencent-tokenhub", display_name="Tencent TokenHub",
        env_key="TOKENHUB_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://tokenhub.tencentmaas.com/v1", base_url_env="TOKENHUB_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="hy3", models=("hy3", "hy3-preview"),
        fallback_eligible=False, builtin=True, prompt_in_init=False,
    ),
    ProviderSpec(
        name="copilot", display_name="GitHub Copilot",
        env_key="COPILOT_GITHUB_TOKEN",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://api.githubcopilot.com", base_url_env="COPILOT_API_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="claude-sonnet-4.6",
        models=("claude-sonnet-4.6", "gpt-5.6-sol", "gemini-3.5-flash"),
        fallback_eligible=False, builtin=True, subscription=True, prompt_in_init=False,
        signup_url="https://github.com/features/copilot",
        # Some agents also accept GH_TOKEN / GITHUB_TOKEN here. We do NOT: those are
        # the generic GitHub CLI variables, set on a large share of developer
        # machines for reasons unrelated to Copilot. Claiming them would make
        # every such box report a "usable" Copilot provider that 403s the moment
        # it is selected. Set COPILOT_GITHUB_TOKEN explicitly.
        tos_note="Copilot is a flat-rate subscription; usage is not metered per token.",
    ),
    # -----------------------------------------------------------------------
    # OAuth subscription seats (2026-08-12) — sign in with a plan you pay for
    # -----------------------------------------------------------------------
    # ⚠ TERMS OF SERVICE. These plans issue no OAuth client_id to third-party
    # applications. The client_ids below are the ones published in the vendors'
    # OWN CLIs (Claude Code, Codex CLI, VS Code, Grok CLI, Qwen CLI), so
    # connecting here authenticates POLYROB as that client. Every other
    # open-source agent that offers "sign in with your Claude/ChatGPT plan"
    # does the same thing — but it is a real exposure and it lands on the account
    # HOLDER, not on us. Each row carries a tos_note, and `polyrob auth add`
    # prints it before running the flow (§7.4). LLM_OAUTH_ENABLED stays OFF by
    # default so this is never entered by accident.
    #
    # These rows carry NO env_key: an OAuth seat is not an API key, and giving
    # them one would make an unrelated variable look like a working credential.
    ProviderSpec(
        name="anthropic-oauth", display_name="Anthropic (Claude Pro/Max)",
        aliases=("claude-max", "claude-pro"),
        auth_type=AuthType.OAUTH_PKCE, env_key=None,
        oauth=OAuthSpec(
            auth_url="https://claude.ai/oauth/authorize",
            token_url="https://platform.claude.com/v1/oauth/token",
            client_id="9d1c250a-e61b-44d9-88ed-5944d1962f5e",
            scopes=("org:create_api_key", "user:profile", "user:inference"),
            grant="authorization_code",
            # Anthropic redirects to a hosted page that DISPLAYS the code —
            # there is no loopback listener, which is also why this one works
            # over SSH.
            redirect_mode="manual",
            redirect_uri="https://console.anthropic.com/oauth/code/callback",
            refresh_skew_sec=120,
        ),
        transport=Transport.ANTHROPIC_MESSAGES,
        base_url="https://api.anthropic.com", base_url_env="ANTHROPIC_OAUTH_BASE_URL",
        # An OAuth seat authenticates Authorization: Bearer, NOT x-api-key —
        # without this the token goes out as an API key and Anthropic returns
        # "invalid x-api-key".
        bearer_auth=True,
        # Anthropic routes OAuth traffic on these headers; without them requests
        # intermittently 500. The betas are what a subscription client must send
        # to use the seat at all.
        # ⚠ The user-agent VERSION has to stay reasonably current — Anthropic
        # rejects OAuth requests whose claimed client version is far behind the
        # real release. If OAuth calls start failing, check this first.
        extra_headers=(
            ("anthropic-beta", "claude-code-20250219,oauth-2025-04-20"),
            ("user-agent", "claude-code/2.1.200 (external, cli)"),
            ("x-app", "cli"),
        ),
        supports_native_tools=True, supports_vision=True,
        default_model="claude-sonnet-4-5", models=("claude-sonnet-4-5",),
        fallback_eligible=False, builtin=True, subscription=True, prompt_in_init=False,
        tos_note=(
            "Connects your Claude Pro/Max seat using Claude Code's OAuth client id — "
            "Anthropic issues none to third-party apps. Your account, your risk."
        ),
    ),
    ProviderSpec(
        name="openai-codex", display_name="OpenAI Codex (ChatGPT plan)",
        aliases=("codex",),
        auth_type=AuthType.OAUTH_DEVICE, env_key=None,
        oauth=OAuthSpec(
            auth_url="https://auth.openai.com/api/accounts/deviceauth/usercode",
            token_url="https://auth.openai.com/oauth/token",
            client_id="app_EMoamEEZ73f0CkXaXp7hrann",
            grant="device_code",
            device_auth_style="json",   # OpenAI's device leg is JSON, not form
            refresh_skew_sec=120,
        ),
        transport=Transport.RESPONSES,
        base_url="https://chatgpt.com/backend-api/codex",
        base_url_env="CODEX_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="gpt-5-codex", models=("gpt-5-codex",),
        fallback_eligible=False, builtin=True, subscription=True, prompt_in_init=False,
        # ⚠ transport RESPONSES has no generic client yet — the row is declared
        # so the connect flow and the catalog are complete, but inference needs
        # the Responses client (see the transport gap in the handoff).
        tos_note=(
            "Connects your ChatGPT plan using the Codex CLI's OAuth client id — "
            "OpenAI issues none to third-party apps. Your account, your risk."
        ),
    ),
    ProviderSpec(
        name="github-copilot", display_name="GitHub Copilot (OAuth)",
        auth_type=AuthType.OAUTH_DEVICE, env_key=None,
        oauth=OAuthSpec(
            auth_url="https://github.com/login/device/code",
            token_url="https://github.com/login/oauth/access_token",
            # VS Code's GitHub App id. Chosen over other public ids because it
            # mints ghu_ tokens, which the Copilot token endpoint will exchange;
            # gho_ tokens from other apps 404 there.
            client_id="Iv1.b507a08c87ecfe98",
            scopes=("read:user",),
            grant="device_code",
            token_exchange_url="https://api.github.com/copilot_internal/v2/token",
            refresh_skew_sec=300,
        ),
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://api.githubcopilot.com", base_url_env="COPILOT_API_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="claude-sonnet-4.6",
        models=("claude-sonnet-4.6", "gpt-5.6-sol", "gemini-3.5-flash"),
        fallback_eligible=False, builtin=True, subscription=True, prompt_in_init=False,
        tos_note=(
            "Connects your Copilot subscription using VS Code's OAuth app id. "
            "Your account, your risk. Key-based access is the `copilot` row."
        ),
    ),
    ProviderSpec(
        name="xai-oauth", display_name="xAI Grok (SuperGrok / X Premium+)",
        auth_type=AuthType.OAUTH_DEVICE, env_key=None,
        oauth=OAuthSpec(
            auth_url="https://auth.x.ai/oauth2/device/code",
            token_url="https://auth.x.ai/oauth2/token",
            client_id="b1a00492-073a-47ea-816f-4c329264a828",
            scopes=("openid", "profile", "email", "offline_access",
                    "grok-cli:access", "api:access"),
            grant="device_code",
            # xAI tokens live ~6h. A 2-minute skew is too tight for a cron or
            # gateway workload that only touches the provider every half hour —
            # it would keep hitting brief expiry gaps. Refresh an hour early.
            refresh_skew_sec=3600,
        ),
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://api.x.ai/v1", base_url_env="XAI_OAUTH_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="grok-4.3", models=("grok-4.5", "grok-4.3"),
        fallback_eligible=False, builtin=True, subscription=True, prompt_in_init=False,
        tos_note=(
            "Connects your SuperGrok / X Premium+ seat using the Grok CLI's "
            "OAuth client id. Your account, your risk."
        ),
    ),
    ProviderSpec(
        name="qwen-oauth", display_name="Qwen (portal subscription)",
        auth_type=AuthType.OAUTH_PKCE, env_key=None,
        oauth=OAuthSpec(
            auth_url="https://chat.qwen.ai/api/v1/oauth2/device/code",
            token_url="https://chat.qwen.ai/api/v1/oauth2/token",
            client_id="f0304373b74a44d2b584a3fb70ca9e56",
            grant="device_code",
            refresh_skew_sec=120,
        ),
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://portal.qwen.ai/v1", base_url_env="QWEN_OAUTH_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="qwen3-coder-plus", models=("qwen3-coder-plus",),
        fallback_eligible=False, builtin=True, subscription=True, prompt_in_init=False,
        tos_note=(
            "Connects your Qwen portal subscription using the Qwen CLI's OAuth "
            "client id. Your account, your risk."
        ),
    ),
    ProviderSpec(
        name="minimax-oauth", display_name="MiniMax (subscription)",
        auth_type=AuthType.OAUTH_DEVICE, env_key=None,
        oauth=OAuthSpec(
            auth_url="https://api.minimax.io/oauth/device/code",
            token_url="https://api.minimax.io/oauth/token",
            client_id="78257093-7e40-4613-99e0-527b14b39113",
            scopes=("group_id", "profile", "model.completion"),
            grant="device_code",
            # MiniMax redeems the code under a NON-standard urn.
            token_grant_type="urn:ietf:params:oauth:grant-type:user_code",
            refresh_skew_sec=60,
        ),
        transport=Transport.ANTHROPIC_MESSAGES,
        base_url="https://api.minimax.io/anthropic",
        base_url_env="MINIMAX_OAUTH_BASE_URL",
        supports_native_tools=True, supports_vision=False,
        default_model="MiniMax-M2.7", models=("MiniMax-M3", "MiniMax-M2.7"),
        fallback_eligible=False, builtin=True, subscription=True, prompt_in_init=False,
        tos_note="Connects your MiniMax subscription seat via OAuth.",
    ),
)


# ---------------------------------------------------------------------------
# User-declared providers (~/.polyrob/providers.yaml)
# ---------------------------------------------------------------------------
# Fields a providers.yaml row may set. client_class_name / builtin /
# fallback_rank are deliberately absent (code-selection and internal ordering
# are never user data).
#
# ``oauth`` IS user-settable (024 L2). It has to be: POLYROB ships no built-in
# OAuth providers, because the plans people want to connect issue no client_id
# to third-party apps and borrowing a vendor CLI's id would make POLYROB
# impersonate their product. So the client_id is the user's to supply. The block
# is validated exactly like base_url (https only, never a metadata endpoint) —
# see _parse_oauth_block.
_USER_SETTABLE_FIELDS = frozenset({
    "display_name", "aliases", "auth_type", "env_key", "transport", "base_url",
    "base_url_env", "extra_headers", "bearer_auth", "supports_native_tools",
    "supports_vision", "default_model", "models", "model_prefixes",
    "fallback_eligible", "initializable", "subscription", "signup_url", "tos_note",
    "prompt_in_init", "oauth", "cache_strategy",
})

_CACHE_STRATEGIES = ("in_client", "automatic", "explicit", "breakpoints", "none")

_GENERIC_CLIENT_FOR_TRANSPORT = {
    Transport.CHAT_COMPLETIONS: "OpenAICompatClient",
    Transport.ANTHROPIC_MESSAGES: "AnthropicCompatClient",
    Transport.RESPONSES: "ResponsesCompatClient",
}


def generic_client_class_name(transport: Transport) -> Optional[str]:
    """The generic client class serving *transport*, or None if unimplemented."""
    return _GENERIC_CLIENT_FOR_TRANSPORT.get(transport)


#: Placeholder api_key for AuthType.NONE providers (Ollama & friends): the
#: OpenAI SDK requires a non-empty key, and LLMManager's bootstrap gates treat
#: a missing api_key as "not configured" — the sentinel satisfies both. Never
#: a real credential.
NO_KEY_SENTINEL = "not-needed"


def user_providers_path(env=None) -> Optional[str]:
    """Path to the user providers file, or None when disabled.

    ``LLM_CUSTOM_PROVIDERS`` unset => ``<polyrob_home>/providers.yaml``;
    set-but-empty => disabled; otherwise the given path.
    """
    env = os.environ if env is None else env
    raw = env.get("LLM_CUSTOM_PROVIDERS")
    if raw is not None:
        raw = str(raw).strip()
        return raw or None
    from core.paths import polyrob_home
    return str(polyrob_home() / "providers.yaml")


def _coerce_str_tuple(value) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


# --- providers.yaml row validation (024 security review C1, 2026-08-07) -----
# A row's env_key/base_url are what the generic clients SEND CREDENTIALS to —
# an unvalidated row could name any process secret (WALLET_MASTER_SEED,
# TELEGRAM_BOT_TOKEN, ...) as its "api key" and ship it to an arbitrary URL.
# env_key must therefore be API-key-shaped, base_url_env URL-shaped, and both
# must avoid known non-LLM secret name tokens; base_url must be http(s) and
# never the cloud metadata endpoint. A violating row is SKIPPED with a WARN
# (loud), never silently narrowed.
import re as _re

_ENV_KEY_RE = _re.compile(r"^[A-Z][A-Z0-9_]*_API_KEY$")
_BASE_URL_ENV_RE = _re.compile(r"^[A-Z][A-Z0-9_]*_URL$")
_SENSITIVE_NAME_TOKENS = (
    "SEED", "MNEMONIC", "WALLET", "PRIVATE", "SECRET", "PASSWORD", "PASSWD",
    "JWT", "TELEGRAM", "TWITTER", "X402", "SMTP", "IMAP", "OAUTH", "MASTER",
    "COOKIE", "SESSION",
)


def _env_name_sensitive(name: str) -> bool:
    upper = name.upper()
    return any(tok in upper for tok in _SENSITIVE_NAME_TOKENS)


def _validate_row_security(name: str, kwargs: Dict[str, object]) -> Optional[str]:
    """Return a rejection reason for a hostile/malformed row, or None if OK."""
    env_key = kwargs.get("env_key")
    if env_key is not None:
        if not _ENV_KEY_RE.match(str(env_key)) or _env_name_sensitive(str(env_key)):
            return (
                f"env_key {env_key!r} is not an API-key-shaped variable name "
                "(must match *_API_KEY and not name a non-LLM secret)"
            )
    base_url_env = kwargs.get("base_url_env")
    if base_url_env is not None:
        if not _BASE_URL_ENV_RE.match(str(base_url_env)) or _env_name_sensitive(str(base_url_env)):
            return (
                f"base_url_env {base_url_env!r} is not a URL-shaped variable name "
                "(must match *_URL and not name a secret)"
            )
    base_url = kwargs.get("base_url")
    if base_url is not None:
        reason = _validate_base_url(str(base_url))
        if reason:
            return reason
    return None


def _parse_oauth_block(row_name: str, value) -> "OAuthSpec":
    """Build an OAuthSpec from a providers.yaml ``oauth:`` mapping.

    Raises ``ValueError`` (caught by the row parser, which then skips the row
    with a WARN) on anything malformed or hostile. Both endpoints go through the
    SAME check as ``base_url`` plus an https requirement: an OAuth endpoint
    receives a client_id and returns a bearer token, so plaintext http would put
    the credential on the wire, and a metadata-service URL would turn the flow
    into an SSRF primitive.
    """
    if not isinstance(value, dict):
        raise ValueError("oauth must be a mapping")
    unknown = set(value) - {"auth_url", "token_url", "client_id", "scopes",
                            "grant", "refresh_skew_sec", "device_auth_style",
                            "redirect_mode", "token_exchange_url", "redirect_uri",
                            "token_user_agent", "token_grant_type"}
    if unknown:
        raise ValueError(f"unknown oauth field(s): {sorted(unknown)}")
    for field_name in ("auth_url", "token_url", "token_exchange_url", "redirect_uri"):
        url = value.get(field_name)
        if url is None:
            continue
        reason = _validate_base_url(str(url))
        if reason:
            raise ValueError(f"oauth.{field_name}: {reason}")
        if not str(url).lower().startswith("https://"):
            raise ValueError(
                f"oauth.{field_name} must be https (a token would cross the "
                "network in plaintext otherwise)"
            )
    try:
        skew = int(value.get("refresh_skew_sec", 120))
    except (TypeError, ValueError):
        raise ValueError("oauth.refresh_skew_sec must be an integer")
    if skew < 0:
        raise ValueError("oauth.refresh_skew_sec must not be negative")
    style = str(value.get("device_auth_style") or "form").strip().lower()
    if style not in ("form", "json"):
        raise ValueError("oauth.device_auth_style must be 'form' or 'json'")
    mode = str(value.get("redirect_mode") or "loopback").strip().lower()
    if mode not in ("loopback", "manual"):
        raise ValueError("oauth.redirect_mode must be 'loopback' or 'manual'")
    return OAuthSpec(
        auth_url=str(value.get("auth_url") or ""),
        token_url=str(value.get("token_url") or ""),
        client_id=str(value.get("client_id") or ""),
        scopes=_coerce_str_tuple(value.get("scopes")),
        grant=str(value.get("grant") or "device_code").strip().lower(),
        refresh_skew_sec=skew,
        device_auth_style=style,
        redirect_mode=mode,
        token_exchange_url=str(value.get("token_exchange_url") or ""),
        redirect_uri=str(value.get("redirect_uri") or ""),
        token_user_agent=str(value.get("token_user_agent") or ""),
        token_grant_type=str(value.get("token_grant_type")
                             or "urn:ietf:params:oauth:grant-type:device_code"),
    )


def _validate_base_url(url: str) -> Optional[str]:
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return f"base_url unparseable ({exc})"
    if parts.scheme not in ("http", "https"):
        return f"base_url scheme {parts.scheme!r} not allowed (http/https only)"
    host = (parts.hostname or "").lower()
    if not host:
        return "base_url has no host"
    if host == "metadata.google.internal" or host.startswith("169.254."):
        return f"base_url host {host!r} is a cloud-metadata endpoint"
    return None


# Queryable load state for the last providers.yaml parse — what `polyrob
# doctor` renders as the "providers file" block (UX assessment 2026-08-07, Q7).
# Shape: {"path", "loaded": [names], "rejected": [(name, reason)], "file_error"}.
# Empty dict = no file configured/present. Reset with the registry cache.
_LOAD_REPORT: Dict[str, object] = {}


def _reject(name: str, reason: str) -> None:
    """Log a row rejection AND record it in the load report (never log-only)."""
    logger.warning("providers.yaml[%s]: %s — row skipped", name, reason)
    _LOAD_REPORT.setdefault("rejected", []).append((str(name), reason))


def user_providers_report() -> Dict[str, object]:
    """The last providers.yaml load outcome (path, loaded, rejected, file_error).

    Ensures the registry has loaded first; empty dict when no file is
    configured/present or the registry is disabled. Consumed by
    ``polyrob doctor`` so rejection feedback is a queryable state, not a
    transient load-time log line.
    """
    get_specs()
    return dict(_LOAD_REPORT)


def _parse_user_row(name: str, row: dict, base: Optional[ProviderSpec]) -> Optional[ProviderSpec]:
    """Build one spec from a providers.yaml row. Returns None (with a WARN +
    load-report entry) on a malformed row — fail-open per entry, never a crash."""
    if not isinstance(row, dict):
        _reject(str(name), "entry is not a mapping")
        return None
    name = str(name).strip().lower()
    if not name or any(c in name for c in "/\\ \t") or name.startswith("."):
        _reject(name, f"invalid provider name {name!r}")
        return None
    if name.endswith("_client"):
        # llm_manager derives provider names by stripping '_client' from service
        # names — a provider literally named '*_client' would alias into another
        # provider's service slot (review M7).
        _reject(name, "provider name may not end in '_client'")
        return None

    kwargs: Dict[str, object] = {}
    for key, value in row.items():
        if key == "client_class_name":
            logger.warning(
                "providers.yaml[%s]: 'client_class_name' is not user-settable "
                "(user rows may only select a generic transport) — ignored", name
            )
            continue
        if key not in _USER_SETTABLE_FIELDS:
            logger.warning("providers.yaml[%s]: unknown field %r — ignored", name, key)
            continue
        try:
            if key == "auth_type":
                kwargs[key] = AuthType(str(value).strip().lower())
            elif key == "transport":
                kwargs[key] = Transport(str(value).strip().lower())
            elif key in ("aliases", "models", "model_prefixes"):
                kwargs[key] = _coerce_str_tuple(value)
            elif key == "extra_headers":
                if not isinstance(value, dict):
                    raise ValueError("extra_headers must be a mapping")
                kwargs[key] = tuple((str(k), str(v)) for k, v in value.items())
            elif key == "oauth":
                kwargs[key] = _parse_oauth_block(name, value)
            elif key == "cache_strategy":
                if value is not None and str(value) not in _CACHE_STRATEGIES:
                    raise ValueError(f"must be one of {_CACHE_STRATEGIES}")
                kwargs[key] = None if value is None else str(value)
            elif key in ("supports_native_tools", "supports_vision", "bearer_auth",
                         "fallback_eligible", "initializable", "subscription",
                         "prompt_in_init"):
                kwargs[key] = bool(value)
            else:
                kwargs[key] = None if value is None else str(value)
        except (ValueError, TypeError) as exc:
            _reject(name, f"bad {key} ({exc})")
            return None

    reason = _validate_row_security(name, kwargs)
    if reason:
        _reject(name, reason)
        return None

    if base is not None:
        # Override of a built-in provider: user fields replace the built-in's,
        # but the client class / builtin provenance / fallback rank are kept.
        # transport is bound to the built-in's bespoke client — never movable.
        # A row that RESTATES the built-in's own transport is a no-op, not a
        # conflict: since 024 T0 ships built-in rows for plans users already
        # hand-authored (zai-coding), warning there would fire on every load of
        # a file that is exactly right. Only a genuine CHANGE is refused.
        if "transport" in kwargs:
            if kwargs["transport"] is not base.transport:
                logger.warning(
                    "providers.yaml[%s]: 'transport' cannot be overridden on a "
                    "built-in provider — ignored", name
                )
            kwargs.pop("transport")
        return replace(base, **kwargs)

    spec = ProviderSpec(name=name,
                        display_name=str(kwargs.pop("display_name", None) or name),
                        builtin=False, client_class_name=None, **kwargs)
    if generic_client_class_name(spec.transport) is None:
        _reject(name, f"transport {spec.transport.value!r} has no generic client yet")
        return None
    if not spec.base_url and not spec.base_url_env:
        # Without an endpoint the generic client would ride its DEFAULT base URL
        # (silent traffic misdirection) — refuse the row (assessment Q14).
        _reject(name, "no base_url (or base_url_env) declared")
        return None
    if not spec.models:
        logger.warning(
            "providers.yaml[%s]: no 'models:' declared — usable only when named "
            "explicitly (-p %s -m <model>); absent from model listings", name, name
        )
    return spec


def _load_user_specs() -> Tuple[ProviderSpec, ...]:
    """Parse providers.yaml (lazy; caller caches). Fail-open on every error."""
    global _LOAD_REPORT
    _LOAD_REPORT = {}
    path = user_providers_path()
    if not path or not os.path.isfile(path):
        return ()
    _LOAD_REPORT = {"path": path, "loaded": [], "rejected": [], "file_error": None}
    # providers.yaml is credential-equivalent (it redirects the agent's own
    # inference endpoint). The secret_guard denial is scoped to the polyrob
    # config homes — warn loudly when the operator relocates it outside them,
    # where agent file tools could reach it.
    parts = {p.lower() for p in os.path.normpath(path).split(os.sep)}
    if ".polyrob" not in parts and ".rob" not in parts:
        logger.warning(
            "LLM_CUSTOM_PROVIDERS points outside a polyrob config home (%s). "
            "This file redirects inference endpoints — keep it under ~/.polyrob "
            "so agent file tools are denied access to it.", path
        )
    try:
        # Group/world-writable providers.yaml = anyone on the box can redirect
        # the agent's inference endpoint (review M6) — refuse to load it.
        mode = os.stat(path).st_mode
        if mode & 0o022:
            logger.warning(
                "providers.yaml %s is group/world-writable (mode %o) — refusing "
                "to load it; chmod 600 to enable", path, mode & 0o777
            )
            _LOAD_REPORT["file_error"] = (
                f"group/world-writable (mode {mode & 0o777:o}) — refusing to load; chmod 600"
            )
            return ()
    except OSError:
        return ()
    try:
        import yaml  # lazy: keep module import light (entry-point weight test)
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.warning("providers.yaml unreadable (%s) — no user providers loaded", exc)
        _LOAD_REPORT["file_error"] = f"unreadable ({exc}) — no user providers loaded"
        return ()
    rows = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        if rows is not None:
            logger.warning("providers.yaml: top-level 'providers' must be a mapping")
            _LOAD_REPORT["file_error"] = "top-level 'providers' must be a mapping"
        return ()
    builtin_by_name = {s.name: s for s in BUILTIN_SPECS}
    out: List[ProviderSpec] = []
    for name, row in rows.items():
        spec = _parse_user_row(name, row, builtin_by_name.get(str(name).strip().lower()))
        if spec is not None:
            # Make every endpoint redirect VISIBLE — a silent redirect is the
            # threat model here (review C1 fix #4).
            logger.info(
                "provider registry: user row %r → base_url=%s env_key=%s transport=%s",
                spec.name, spec.resolved_base_url(), spec.env_key, spec.transport.value,
            )
            out.append(spec)
            _LOAD_REPORT.setdefault("loaded", []).append(spec.name)
    return tuple(out)


# ---------------------------------------------------------------------------
# The merged registry (cached; reset via reset_provider_registry_cache)
# ---------------------------------------------------------------------------
_SPECS_CACHE: Optional[Tuple[ProviderSpec, ...]] = None


def _mark_uninitializable_without_a_client(spec: ProviderSpec) -> ProviderSpec:
    """Force ``initializable=False`` when nothing can construct a client.

    A spec declaring a transport with no generic client (RESPONSES today) and no
    bespoke ``client_class_name`` is absent from ``PROVIDER_CONFIG`` — so it
    would be reported USABLE by the gating oracles, auto-selected by the
    resolver, and then die in ``create_llm_client`` with "Unknown LLM client
    type". Derived rather than hand-set: the next row to declare an
    unimplemented transport gets the same protection for free.
    """
    if spec.initializable and spec.client_class_name is None \
            and generic_client_class_name(spec.transport) is None:
        return replace(spec, initializable=False)
    return spec


def get_specs() -> Tuple[ProviderSpec, ...]:
    """All provider specs, canonical order: built-ins (legacy PROFILES order,
    user overrides applied in place), then new user-declared providers.
    """
    global _SPECS_CACHE
    if _SPECS_CACHE is not None:
        return _SPECS_CACHE
    user = _load_user_specs()
    overrides = {s.name: s for s in user if any(b.name == s.name for b in BUILTIN_SPECS)}
    merged: List[ProviderSpec] = [overrides.get(b.name, b) for b in BUILTIN_SPECS]
    merged.extend(s for s in user if s.name not in overrides)
    _SPECS_CACHE = tuple(_mark_uninitializable_without_a_client(m) for m in merged)
    return _SPECS_CACHE


def get_spec(name: str) -> Optional[ProviderSpec]:
    """Spec by canonical NAME first, then by alias (case-insensitive).

    Two passes, and the order matters: a real provider must never be shadowed by
    an earlier row that happens to claim its name as an alias. That is not
    hypothetical — shipping a `zai` provider alongside `zai-coding` (which had
    `zai` as an alias) silently routed every `zai` lookup to the coding-plan
    row, so an OpenAI-compatible endpoint got the Anthropic schema generator and
    would have shipped tools the provider could not parse.
    """
    if not name:
        return None
    low = str(name).strip().lower()
    specs = get_specs()
    for spec in specs:
        if spec.name == low:
            return spec
    for spec in specs:
        if low in spec.aliases:
            return spec
    return None


def canonicalize_provider(name: Optional[str]) -> Optional[str]:
    """Canonical spec NAME for a provider name-or-alias; unknown/empty → unchanged.

    The LLM manager registers clients under spec names only, so any surface that
    accepts an alias (``glm`` → ``zai``, ``kimi`` → ``moonshot``) must pass it
    through here before the client lookup — otherwise a valid key still dies as
    "No client found for provider glm". Unknown names pass through as typed so a
    custom/undeclared provider errors honestly downstream.
    """
    spec = get_spec(name) if name else None
    return spec.name if spec is not None else name


def user_declared_specs() -> Tuple[ProviderSpec, ...]:
    return tuple(s for s in get_specs() if not s.builtin)


def is_flat_rate(provider: Optional[str]) -> bool:
    """True when *provider*'s plan is flat-rate, so per-token cost is meaningless.

    THE consumer of ``ProviderSpec.subscription`` (before 024 T0 the field was
    declared and read by nothing). A flat-rate seat bills a fixed
    monthly/quarterly fee — or GPU-time under it — so multiplying this call's
    tokens by a per-token price does not describe any real charge. The billing
    entry point (``modules.credits.pricing.compute_llm_cost``) therefore records
    $0 marginal cost for these providers instead of a fabricated figure.

    Fail-open ``False``: an unknown provider, a disabled registry, or any
    lookup error means "meter it" — the safe direction, since the worst case is
    an overstated cost rather than a silently-free one.
    """
    if not provider:
        return False
    try:
        spec = get_spec(provider)
    except Exception:
        return False
    return bool(spec is not None and spec.subscription)


def needs_synthetic_config_block(spec: ProviderSpec) -> bool:
    """True when *spec* has no hand-written ``BotConfig.get_llm_config()`` block.

    The six legacy built-ins each have a literal block there (keyed off a
    pydantic ``*_api_key`` field) and a bespoke ``client_class_name``. Every
    other spec — a providers.yaml row OR a newer built-in served by the generic
    transport clients — has neither, so ``extra_llm_config_blocks`` must
    synthesize its ``{api_key, base_url, auth_type}`` block. Without one,
    ``LLMManager._initialize`` skips the provider entirely (its bootstrap gate
    is ``config_data.get('api_key')``) and the provider is silently unusable.

    ``client_class_name is None`` is the exact discriminator: naming a bespoke
    client is what makes a spec dependent on a literal config block.
    """
    return spec.client_class_name is None


def reset_provider_registry_cache() -> None:
    """Clear every registry-derived cache (test isolation / config reload)."""
    global _SPECS_CACHE, _LOAD_REPORT
    _SPECS_CACHE = None
    _LOAD_REPORT = {}
    # Downstream derivation caches (lazy imports: no hard coupling, no cycles).
    try:
        from modules.llm import profiles
        profiles.PROFILES.reset()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        from modules.llm import llm_client_registry
        llm_client_registry.AVAILABLE_MODELS.reset()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        from modules.llm import model_registry
        model_registry.PROVIDER_CONFIG._config = None
    except Exception:
        pass
