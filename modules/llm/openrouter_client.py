"""OpenRouter LLM API Client.

OpenRouter provides unified access to multiple AI models through a single API.
API is OpenAI-compatible, supporting Grok, Kimi, Qwen, and many others.

Docs: https://openrouter.ai/docs/quickstart

Refactored Dec 2025: All model configuration comes from model_registry.
No deprecated fallback constants - registry is the single source of truth.
"""

import logging
import re
import time
import json
from typing import List, Dict, Any, Optional, Union, Tuple
from uuid import uuid4
from openai import AsyncOpenAI

from modules.llm.llm_client import LLMClient, translate_llm_error
from modules.llm.token_counter import count_messages_tokens
from modules.llm.model_registry import get_model_config, ModelProvider
from core.exceptions import LLMError, ServiceError, LLMRateLimitError, LLMAuthenticationError
from core.config import BotConfig

# Import default model from registry (mirrors anthropic_client.py's pattern; no
# circular import — llm_client_registry only imports openrouter_client lazily,
# inside function bodies, never at module level).
from modules.llm.llm_client_registry import get_default_model

logger = logging.getLogger(__name__)

# Kimi-K2 emits tool calls with special delimiter tokens. When a serving layer
# (notably NVIDIA NIM) doesn't parse them, they leak into ``message.content`` as
# raw text with no structured ``tool_calls`` — costing the agent the actions and
# dumping the tokens to output. Recover them client-side.
#   <|tool_call_begin|> functions.{name}:{idx} <|tool_call_argument_begin|> {json} <|tool_call_end|>
#
# We locate each call's *header* (name + idx + the argument-begin marker) with a
# narrow regex, then decode the JSON args with ``json.JSONDecoder().raw_decode``
# from the first ``{`` so a complete JSON object is consumed regardless of any
# trailing tokens. The old ``(?P<args>\{.*?\})`` group truncated when a string
# value literally contained ``} <|tool_call_end|>`` (Bug 3).
_KIMI_TOOL_CALL_HEADER_RE = re.compile(
    r"<\|tool_call_begin\|>\s*functions\.(?P<name>[\w.\-]+):(?P<idx>\d+)\s*"
    r"<\|tool_call_argument_begin\|>\s*",
    re.DOTALL,
)
#: Any Kimi tool-call control token — marks where the parseable content ends.
_KIMI_TOKEN_MARKER = "<|tool_call"


def _new_recovered_call_id(tag: str, i: int) -> str:
    """Mint a GLOBALLY unique id for a recovered tool call (P0-4).

    Recovered ids used to be deterministic per-response (``call_{i}_{idx}`` /
    ``call_txt_{i}``), so two recovery turns in one session produced DUPLICATE
    ids across history. Downstream, ``tool_message_repair.repair_tool_message_pairs``
    keys its ``tool_msg_map`` by id over the WHOLE history (last write wins) and
    ``del``s the id on first use — so step 1's AIMessage got paired with step 2's
    tool result and step 2 got a fabricated "[ERROR: No response recorded...]"
    placeholder. Textual leaks fire on ~20-25% of Kimi/NIM turns, making the
    collision routine.

    Mirrors the ``tool_call_builder.normalize_tool_call`` reference pattern
    (uuid4 for missing ids), kept short/provider-safe: ``call_{tag}_{hex8}_{i}``.
    The trailing enumeration index ``i`` keeps ids distinct within one response
    even in the astronomically-unlikely event of a hex collision, and preserves
    call ordering for debuggability.
    """
    return f"call_{tag}_{uuid4().hex[:8]}_{i}"


def parse_kimi_tool_calls(content: str) -> List[Dict[str, Any]]:
    """Extract OpenAI-shape tool calls from Kimi's leaked delimiter tokens.

    Returns ``[]`` when *content* carries no (valid) Kimi tool-call tokens, so it
    is a safe no-op for every other model/provider. Args that don't parse as JSON
    are skipped (with a distinct WARN) rather than guessed.

    Recovered ids are **globally unique** via ``_new_recovered_call_id`` (uuid4
    suffix). This subsumes the earlier per-response uniqueness fix (Bug 1: Kimi's
    ``idx`` restarts at 0 per function name, so two parallel calls could collide
    within one response) AND the cross-response collision (P0-4: deterministic
    ids repeated across recovery turns, corrupting ``tool_message_repair``'s
    id-keyed history map). The enumeration index ``i`` is retained for ordering
    and intra-response distinctness.
    """
    text = content or ""
    out: List[Dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for i, m in enumerate(_KIMI_TOOL_CALL_HEADER_RE.finditer(text)):
        name = m.group("name")
        idx = m.group("idx")
        # Args start at the first '{' after the argument-begin marker. raw_decode
        # consumes exactly one complete JSON object and ignores trailing tokens,
        # so an embedded ``} <|tool_call_end|>`` inside a string value no longer
        # truncates the capture.
        brace = text.find("{", m.end())
        if brace == -1:
            logger.warning(
                f"event=kimi_toolcall_parse_error model=? name={name} idx={idx} "
                f"reason=no_opening_brace (Kimi tool-call header without JSON args — dropped)"
            )
            continue
        try:
            obj, end = decoder.raw_decode(text, brace)
        except (ValueError, TypeError) as e:
            logger.warning(
                f"event=kimi_toolcall_parse_error name={name} idx={idx} "
                f"reason=json_decode_failed err={e} (Kimi tool-call args did not parse — dropped)"
            )
            continue
        out.append({
            "id": _new_recovered_call_id("kimi", i),
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(obj)},
        })
    return out


def strip_kimi_tokens(content: str) -> str:
    """Drop everything from the first Kimi control token onward.

    The brain-state text precedes the token soup, so this keeps it for brain
    extraction while removing the unparsed tool-call tokens from what streams.
    """
    if not content:
        return content
    cut = content.find(_KIMI_TOKEN_MARKER)
    return content[:cut].rstrip() if cut != -1 else content


def recover_kimi_content(content: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Reconcile leaked Kimi control tokens in *content* into clean output.

    Returns ``(cleaned_content, recovered_tool_calls)``. When no Kimi control
    token is present this is a no-op: ``(content, [])``.

    The key invariant (WS-2.1): **stripping is unconditional once any token
    leaks**, independent of whether structured calls were recovered. Three leak
    shapes were seen from kimi-k2.6 on NVIDIA NIM:
      1. pure brain JSON (no tokens)                  → no marker, untouched
      2. brain JSON + full ``<|tool_call_begin|>`` blocks → calls recovered + stripped
      3. brain JSON + stray ``<|tool_call_end|>`` only   → 0 calls but STILL stripped
    The old code only stripped in case (2), so case (3) leaked raw tokens to the
    user. This always strips, so the tokens become structurally impossible to leak.
    """
    if not content or _KIMI_TOKEN_MARKER not in content:
        return (content, [])
    return (strip_kimi_tokens(content), parse_kimi_tool_calls(content))


# ---------------------------------------------------------------------------
# Textual tool-call recovery (B4, 2026-06-16). kimi-k2.6 on NVIDIA NIM ALSO
# leaks tool calls in two NON-pipe-token shapes when the serving layer fails to
# emit structured ``tool_calls`` — both captured live:
#   A. Anthropic XML:  <invoke name="done"><parameter name="text">hi</parameter></invoke>
#   B. python-call:    done(text="hi")        (recovered only for KNOWN tool names)
# The pipe-token path (recover_kimi_content) never sees these. Left unrecovered,
# the action is LOST (empty-action loop) AND the raw text dumps to the user as a
# "rob" bubble (the leak shapes end in ``)``/``>`` so the render-layer brain-state
# check doesn't demote them). recover_textual_tool_calls reconciles both shapes.
# ---------------------------------------------------------------------------

_XML_INVOKE_RE = re.compile(
    r"<invoke\b[^>]*\bname=\"(?P<name>[\w.\-]+)\"[^>]*>(?P<body>.*?)</invoke>",
    re.DOTALL,
)
_XML_PARAM_RE = re.compile(
    r"<parameter\b[^>]*\bname=\"(?P<key>[\w.\-]+)\"[^>]*>(?P<val>.*?)</parameter>",
    re.DOTALL,
)
#: Stray/partial function-call XML tags to strip even when no full block parses
#: (mirrors the WS-2.1 "always strip" invariant for pipe tokens — a lone
#: ``</invoke>`` must never reach the user).
_XML_TOOLCALL_TAG_RE = re.compile(
    r"</?(?:function_calls|invoke|parameter)\b[^>]*>", re.DOTALL
)
#: GLM/zhipu leak: a whole ``<function_calls> ... </function_calls>`` dump (tool name
#: as tag + ``<arg_key>``/``<arg_value>`` children, frequently malformed). The args
#: can't be reliably recovered from the malformed shape, but the block must be
#: stripped wholesale so raw tool-call XML never reaches the user. Non-greedy to the
#: first close tag, or to end-of-string when GLM omits/mangles the close.
_FUNCTION_CALLS_BLOCK_RE = re.compile(
    r"<function_calls\b.*?(?:</function_calls>|$)", re.DOTALL
)


def _has_toolcall_xml_marker(content: Optional[str]) -> bool:
    """True if content carries a tool-call XML marker (provider-agnostic leak signal).

    Prose virtually never contains these literal markers, so their presence is a
    reliable signal to run textual-tool-call recovery/stripping for ANY provider —
    not just Kimi (GLM leaks ``<function_calls>``; Anthropic-style leaks ``<invoke>``).
    """
    if not content:
        return False
    return "<function_calls" in content or "<invoke" in content


def tool_names_from_schemas(tools: Optional[List[Dict[str, Any]]]) -> set:
    """Extract the set of callable tool names from OpenAI-format tool schemas."""
    names: set = set()
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else None
        name = (fn or {}).get("name") or t.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _coerce_param_value(raw: str) -> Any:
    """Best-effort coerce an XML ``<parameter>`` body to a JSON value, else str."""
    s = (raw or "").strip()
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return raw


def _parse_call_args(inner: str) -> Optional[Dict[str, Any]]:
    """Parse the argument list of a textual ``name(...)`` call.

    Accepts ``{json}`` (a single JSON object) or ``key=<json-value>`` kwargs
    (the common ``text="..."`` shape). Returns ``None`` when nothing parses, so a
    bare prose ``foo(bar)`` mention isn't misread as a tool call.
    """
    s = (inner or "").strip()
    if not s:
        return {}
    decoder = json.JSONDecoder()
    if s.startswith("{"):
        try:
            obj, _ = decoder.raw_decode(s)
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            return None
        return None
    args: Dict[str, Any] = {}
    kw_re = re.compile(r"\s*(?P<key>[\w.\-]+)\s*=\s*")
    i, n = 0, len(s)
    while i < n:
        m = kw_re.match(s, i)
        if not m:
            break
        key = m.group("key")
        j = m.end()
        if j < n and s[j] in '"[{':
            try:
                val, end = decoder.raw_decode(s, j)
            except (ValueError, TypeError):
                break
            args[key] = val
            i = end
        else:
            k = s.find(",", j)
            if k == -1:
                k = n
            token = s[j:k].strip()
            try:
                args[key] = json.loads(token)
            except (ValueError, TypeError):
                args[key] = token
            i = k
        while i < n and s[i] in ", \n\t":
            i += 1
    return args or None


def _find_balanced_paren(text: str, open_idx: int) -> int:
    """Index of the ``)`` matching the ``(`` at *open_idx*, respecting double-
    quoted strings so a ``)`` inside a string value doesn't close early.
    Returns -1 when unbalanced."""
    depth = 0
    in_str = False
    esc = False
    for i in range(open_idx, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def recover_textual_tool_calls(
    content: str, known_names: Optional[set] = None
) -> Tuple[str, List[Dict[str, Any]]]:
    """Recover XML / python-call tool calls leaked as raw text; strip them out.

    Returns ``(cleaned_content, tool_calls)``; a no-op returns ``(content, [])``.
    Only ``known_names`` are recovered from the ``name(...)`` shape so prose that
    merely mentions a tool isn't misread as a call. The ``name(...)`` scan starts
    AFTER any leading brain-state JSON object, so a tool name quoted inside the
    brain JSON can't trigger a spurious call (or corrupt the JSON on strip).
    """
    if not content:
        return (content, [])
    found: List[Tuple[int, int, str, Dict[str, Any]]] = []

    # A. Anthropic <invoke name="...">...</invoke> blocks (whole content).
    for m in _XML_INVOKE_RE.finditer(content):
        body = m.group("body")
        args: Dict[str, Any] = {}
        for pm in _XML_PARAM_RE.finditer(body):
            args[pm.group("key")] = _coerce_param_value(pm.group("val"))
        if not args:
            parsed = _parse_call_args(body)
            if parsed:
                args = parsed
        found.append((m.start(), m.end(), m.group("name"), args))

    # B. python-call name(...) for KNOWN names — only when no XML block matched,
    #    and only over the tail AFTER a leading brain-state JSON object.
    if not found and known_names:
        scan_start = 0
        lead = len(content) - len(content.lstrip())
        tail = content[lead:]
        if tail.startswith("{"):
            try:
                _obj, end = json.JSONDecoder().raw_decode(tail)
                scan_start = lead + end
            except (ValueError, TypeError):
                scan_start = 0
        region = content[scan_start:]
        for name in known_names:
            for m in re.finditer(rf"(?<![\w.]){re.escape(name)}\s*\(", region):
                open_idx = region.index("(", m.start())
                close = _find_balanced_paren(region, open_idx)
                if close == -1:
                    continue
                args = _parse_call_args(region[open_idx + 1 : close])
                if args is None:
                    continue
                found.append(
                    (scan_start + m.start(), scan_start + close + 1, name, args)
                )

    found.sort(key=lambda f: f[0])
    calls = [
        {
            "id": _new_recovered_call_id("txt", i),
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        for i, (_s, _e, name, args) in enumerate(found)
    ]

    cleaned = content
    for start, end, _n, _a in sorted(found, key=lambda f: f[0], reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]
    # GLM/zhipu: strip any whole <function_calls> dump block (incl. its malformed
    # inner tool-name / arg_key / arg_value tags) so raw XML never reaches the user.
    cleaned = _FUNCTION_CALLS_BLOCK_RE.sub("", cleaned)
    # Backstop: strip any stray/partial function-call XML tags left behind.
    cleaned = _XML_TOOLCALL_TAG_RE.sub("", cleaned).strip()
    return (cleaned, calls)


class OpenRouterClient(LLMClient):
    """OpenRouter LLM client - unified access to Grok, Kimi, Qwen, and more.

    OpenRouter uses OpenAI-compatible API format, so this client extends
    the patterns from OpenAIClient while adding OpenRouter-specific headers.

    All model capabilities and limits come from model_registry.
    """

    # OpenRouter API base URL (fallback; the declarative source is ProviderProfile — P8)
    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

    # Human-readable provider name used in log/exception messages. Subclasses that
    # reuse this OpenAI-compatible pipeline (e.g. NvidiaClient) override it so their
    # logs/errors name the real provider instead of "OpenRouter".
    _PROVIDER_LABEL = "OpenRouter"

    def _profile_base_url(self) -> str:
        """Transport base URL sourced from the declarative ProviderProfile (P8),
        falling back to the hardcoded constant if the profile is unavailable.

        Delegates to the base ``_resolve_profile_base_url`` helper (profiles.py
        is the single source of truth); keeps the class-level constant as the
        fallback so no prior call sites need to change.
        """
        return self._resolve_profile_base_url("openrouter") or self.OPENROUTER_BASE_URL

    def __init__(self, config: BotConfig, name: str = "openrouter_client"):
        """Initialize the OpenRouter client."""
        super().__init__(config=config, name=name)
        self._client = None

        # Get OpenRouter config
        openrouter_config = config.get_llm_config().get('openrouter', {})
        self.api_key = openrouter_config.get('api_key')
        # Default to the registry's SSOT default (P0.7: was a stale hardcoded
        # literal that had drifted from DEFAULT_MODELS['openrouter']). Honors the
        # POLYROB_OPENROUTER_MODEL env override via get_default_model().
        self.model_type = openrouter_config.get('model') or get_default_model('openrouter')

        # OpenRouter-specific headers for app attribution
        self.site_url = openrouter_config.get('site_url', '')
        self.site_name = openrouter_config.get('site_name', 'POLYROB AI Agent')

        self.last_response = None

        # Get model config from registry (single source of truth)
        model_config = get_model_config(self.model_type)
        if model_config:
            self.max_tokens = model_config.max_completion_tokens
        else:
            # Fallback defaults for OpenRouter (Grok defaults)
            self.max_tokens = 32768
            self.logger.warning(f"Model '{self.model_type}' not found in registry, using defaults")

        # Resolve vision support via base helper (falls back to True if not in registry)
        self.supports_vision = self._resolve_supports_vision()

        self.temperature = 0.7  # OpenRouter default

        self.logger.debug(
            f"OpenRouter client initialized: model={self.model_type}, "
            f"max_tokens={self.max_tokens}, supports_vision={self.supports_vision}"
        )

    def _supports_tools(self) -> bool:
        """Check if current model supports tool calling via model_registry."""
        config = get_model_config(self.model_type)
        if config and config.capabilities:
            return config.capabilities.supports_function_calling
        return True  # Most OpenRouter models support tools

    def _validate_llm_config(self) -> None:
        """Validate LLM config."""
        if not self.api_key:
            raise ServiceError(f"{self._PROVIDER_LABEL} API key not provided")

    async def _setup_client(self) -> None:
        """Set up the OpenRouter client using OpenAI SDK."""
        try:
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self._profile_base_url(),  # P8: profile-sourced transport URL
                default_headers=self._get_openrouter_headers()
            )
            self.logger.debug(f"{self._PROVIDER_LABEL} client setup completed")
        except Exception as e:
            raise ServiceError(f"Failed to set up {self._PROVIDER_LABEL} client: {e}")

    def _get_openrouter_headers(self) -> Dict[str, str]:
        """Get OpenRouter-specific headers for app attribution."""
        headers = {}
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.site_name:
            headers["X-Title"] = self.site_name
        return headers

    async def _validate_connection(self) -> None:
        """Validate OpenRouter connection."""
        try:
            response = await self._client.chat.completions.create(
                model=self.model_type,
                messages=[{"role": "user", "content": "Test"}],
                max_tokens=5
            )
            if not response or not response.choices:
                raise LLMError(f"No valid response from {self._PROVIDER_LABEL} API")
            self.logger.debug(f"{self._PROVIDER_LABEL} API connection validated successfully")
        except Exception as e:
            self.logger.error(f"Failed to validate {self._PROVIDER_LABEL} connection: {e}")
            raise ServiceError(f"Failed to validate {self._PROVIDER_LABEL} connection: {e}")

    async def _cleanup_client(self) -> None:
        """Clean up OpenRouter client."""
        if self._client:
            if hasattr(self._client, 'close'):
                await self._client.close()
            self._client = None
            self.logger.debug(f"{self._PROVIDER_LABEL} client resources released")

    async def _initialize(self) -> None:
        """Initialize the client."""
        if self._initialized:
            return

        try:
            # Log initialization attempt
            self.logger.info(f"Initializing {self._PROVIDER_LABEL} client with model {self.model_type}")

            # Validate API key
            self._validate_llm_config()

            # Setup client
            await self._setup_client()

            # Test connection (skipped for fast startup; validates lazily)
            if not self._skip_validate:
                await self._validate_connection()

            # Mark as initialized
            self._initialized = True

            # Log successful initialization
            self.logger.info(f"✨ {self._PROVIDER_LABEL} client initialized successfully with model {self.model_type}")

        except Exception as e:
            # Single error log with clear cause
            error_msg = f"Failed to initialize {self._PROVIDER_LABEL} client: {str(e)}"
            self.logger.error(error_msg)
            raise ServiceError(error_msg)

    async def _generate(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> str:
        """Generate a response from OpenRouter API."""
        start_time = time.time()

        try:
            if not self._initialized:
                await self._initialize()

            # Format messages
            openrouter_messages = []
            for msg in messages:
                role = msg.get('role', 'user')
                if role == 'ai':
                    role = 'assistant'

                message_dict = {"role": role, "content": msg.get('content', '')}

                # Handle tool messages
                if role == 'tool' and 'tool_call_id' in msg:
                    message_dict["tool_call_id"] = msg["tool_call_id"]
                if role == 'assistant' and 'tool_calls' in msg:
                    message_dict["tool_calls"] = msg["tool_calls"]

                openrouter_messages.append(message_dict)

            temp = temperature if temperature is not None else self.temperature
            max_tokens_value = self._adjust_max_tokens(messages, max_tokens)

            # Make request
            request_params = {
                'model': self.model_type,
                'messages': openrouter_messages,
                'max_tokens': max_tokens_value,
                'temperature': temp
            }

            self.logger.debug(f"{self._PROVIDER_LABEL} API request: model={self.model_type}, max_tokens={max_tokens_value}")

            self.last_response = await self._client.chat.completions.create(**request_params)

            # Extract response
            response_text = ""
            if self.last_response.choices:
                choice = self.last_response.choices[0]
                if choice.message:
                    response_text = choice.message.content or ""

            self._extract_usage_and_capture_telemetry(start_time, True, None, kwargs.get('metadata'))
            return response_text

        except Exception as e:
            self._extract_usage_and_capture_telemetry(start_time, False, str(e), kwargs.get('metadata'))
            self.logger.error(f"{self._PROVIDER_LABEL} API error: {e}")

            # Route through the unified classifier; fall back to ServiceError for generic
            # errors (preserving the original observable contract at this call site).
            translated = translate_llm_error(e, self._PROVIDER_LABEL)
            if type(translated) is LLMError:
                raise ServiceError(f"{self._PROVIDER_LABEL} API error: {e}")
            raise translated

    async def generate_response(
        self,
        prompt: Optional[Union[str, Dict[str, Any]]] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> str:
        """Generate a response from the LLM."""
        try:
            # Handle prompt formats
            if messages is not None:
                formatted_messages = messages
            elif isinstance(prompt, list) and all(isinstance(m, dict) for m in prompt):
                formatted_messages = prompt
            elif isinstance(prompt, dict) and 'messages' in prompt:
                formatted_messages = prompt['messages']
            elif isinstance(prompt, str):
                formatted_messages = [{"role": "user", "content": prompt}]
            else:
                formatted_messages = [{"role": "user", "content": "Hello"}]

            # Add system message
            if system:
                has_system = any(msg.get('role') == 'system' for msg in formatted_messages)
                if not has_system:
                    formatted_messages.insert(0, {"role": "system", "content": system})

            return await self._generate(
                messages=formatted_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                metadata=metadata,
                **kwargs
            )

        except Exception as e:
            self.logger.error(f"Failed to generate response: {e}")
            raise ServiceError(f"Failed to generate response: {e}")

    async def _generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Optional[int]]]:
        """Generate response with tool calling support.

        Args:
            messages: List of messages
            tools: List of tools in OpenAI format
            system: System prompt
            temperature: Temperature for generation
            max_tokens: Maximum tokens for generation
            metadata: Request metadata
            **kwargs: Additional parameters

        Returns:
            Tuple of (text_response, tool_calls, usage_data)
        """
        start_time = time.time()

        try:
            if not self._initialized:
                await self._initialize()

            # Format messages
            formatted_messages = []
            if system:
                formatted_messages.append({"role": "system", "content": system})

            for msg in messages:
                formatted_msg = msg.copy()
                role = msg.get('role', 'user')
                if role == 'ai':
                    formatted_msg['role'] = 'assistant'

                # Convert tool calls to OpenAI format if needed
                if msg.get("role") == "assistant" and "tool_calls" in msg:
                    tool_calls = []
                    for tc in msg.get("tool_calls", []):
                        if not isinstance(tc.get("function"), dict):
                            if "name" in tc and "args" in tc:
                                tool_calls.append({
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tc["name"],
                                        "arguments": json.dumps(tc["args"]) if not isinstance(tc.get("args"), str) else tc["args"]
                                    }
                                })
                            else:
                                tool_calls.append(tc)
                        else:
                            tool_calls.append(tc)
                    formatted_msg["tool_calls"] = tool_calls
                formatted_messages.append(formatted_msg)

            temp = temperature if temperature is not None else self.temperature
            max_tokens_value = self._adjust_max_tokens(messages, max_tokens)

            # P1-3: mark the stable system prefix cacheable for breakpoint-style models
            # (Anthropic/Gemini via OpenRouter). No-op unless OPENROUTER_PROMPT_CACHE is on.
            from modules.llm.cache_hints import apply_openrouter_cache_control
            formatted_messages = apply_openrouter_cache_control(formatted_messages, self.model_type)

            request_params = {
                'model': self.model_type,
                'messages': formatted_messages,
                'max_tokens': max_tokens_value,
                'temperature': temp
            }

            if tools:
                # Ensure tools is a list (not dict) in OpenAI format
                if isinstance(tools, dict):
                    self.logger.warning(f"Tools passed as dict, expected list. This may indicate a schema generator issue.")
                    # Try to extract list if it's wrapped in a dict
                    if 'function_declarations' in tools:
                        # Gemini format - convert to OpenAI format
                        tools = [{"type": "function", "function": f} for f in tools['function_declarations']]
                    else:
                        # Unknown dict format - skip tools
                        self.logger.error(f"Cannot convert tools dict to list: {list(tools.keys())}")
                        tools = None

                if tools:
                    # UP-08: extend the cache breakpoint over the tools block for
                    # breakpoint-style models (no-op unless OPENROUTER_PROMPT_CACHE on).
                    from modules.llm.cache_hints import apply_openrouter_tools_cache_control
                    tools = apply_openrouter_tools_cache_control(tools, self.model_type)
                    request_params['tools'] = tools
                    request_params['tool_choice'] = 'auto'

            self.logger.info(f"[_generate_with_tools] Starting API request: model={self.model_type}, tools={len(tools) if tools else 0}, messages={len(formatted_messages)}")

            api_start = time.time()
            response = await self._client.chat.completions.create(**request_params)
            api_duration = time.time() - api_start

            self.last_response = response
            self.logger.info(f"[_generate_with_tools] API request completed in {api_duration:.1f}s")

            message = response.choices[0].message
            usage_data = self._extract_usage_data()

            if hasattr(message, 'tool_calls') and message.tool_calls:
                content = message.content or ""
                tool_calls_list = []
                for tc in message.tool_calls:
                    # Log raw arguments for debugging MCP nested args issue
                    raw_args = tc.function.arguments
                    self.logger.info(f"[RAW_TOOL_CALL] {tc.function.name}: arguments type={type(raw_args).__name__}, value={raw_args[:500] if isinstance(raw_args, str) else raw_args}")

                    tool_calls_list.append({
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": raw_args
                        }
                    })
                self.logger.debug(f"Extracted {len(tool_calls_list)} tool calls from response")
                return (content, tool_calls_list, usage_data)

            # Fallback: serving layers (notably NVIDIA NIM with Kimi-K2)
            # intermittently fail to parse Kimi's native tool-call tokens and
            # return them as raw text in content with no structured tool_calls.
            # Recover any calls AND always strip the tokens (WS-2.1) so the agent
            # executes the actions (instead of wasting a planning turn) and the
            # control tokens never leak — even in the stray-end-token shape that
            # recovers 0 calls.
            raw_content = message.content or ""
            # L3 FIX: only run Kimi token recovery for Kimi models. The marker
            # ("<|tool_call") is only ever leaked by Kimi-K2 on NIM; gating on the
            # model means a non-Kimi OpenRouter model (Grok/Qwen/…) that legitimately
            # quotes that literal in prose isn't silently truncated.
            cleaned_content, recovered = raw_content, []
            if "kimi" in (self.model_type or "").lower():
                cleaned_content, recovered = recover_kimi_content(raw_content)
            # B4 + GLM leak: also recover/strip tool calls leaked as <invoke> XML,
            # GLM <function_calls> dumps, or done(...) python-call syntax. The pipe
            # pass (Kimi-only above) handles <|tool_call| tokens; this handles XML.
            # Marker-gated (provider-agnostic): a <function_calls>/<invoke> marker in
            # content is a reliable leak signal that prose ~never contains — so GLM
            # (z-ai) leaks are now recovered/stripped too, not just Kimi. No marker =>
            # untouched (byte-identical for the common path).
            if not recovered and (
                _has_toolcall_xml_marker(cleaned_content)
                or "kimi" in (self.model_type or "").lower()
            ):
                cleaned_content, textual = recover_textual_tool_calls(
                    cleaned_content, tool_names_from_schemas(tools)
                )
                recovered = textual
            if cleaned_content != raw_content or recovered:
                # R4: emit a structured, greppable breadcrumb so a Kimi recovery is
                # observable in telemetry/logs at PARSE time (the structured-provider
                # path has no equivalent silent step). Stable marker: kimi_toolcall_recovery.
                if recovered:
                    self.logger.warning(
                        f"event=kimi_toolcall_recovery model={self.model_type} "
                        f"recovered_calls={len(recovered)} action=recovered "
                        f"({self._PROVIDER_LABEL}: recovered Kimi tool call(s) from unparsed "
                        f"content tokens — serving-layer parser miss)"
                    )
                else:
                    self.logger.warning(
                        f"event=kimi_toolcall_recovery model={self.model_type} "
                        f"recovered_calls=0 action=stripped_only "
                        f"({self._PROVIDER_LABEL}: stripped leaked Kimi control token(s) with no "
                        f"recoverable tool calls — serving-layer parser miss)"
                    )
            return (cleaned_content, recovered, usage_data)

        except Exception as e:
            self._extract_usage_and_capture_telemetry(start_time, False, str(e), metadata)
            self.logger.error(f"{self._PROVIDER_LABEL} tool generation failed: {e}")
            raise LLMError(f"{self._PROVIDER_LABEL} generation failed: {e}")

    async def generate_agent_response(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs
    ) -> Union[Tuple[str, List[Dict[str, Any]], Dict[str, Optional[int]]], str]:
        """Generate response with tool-calling support for agents.

        This method bridges the adapter contract to enable native tool-calling
        for OpenRouter models. It delegates to _generate_with_tools which handles the
        wire-format conversion and API interaction.

        Args:
            messages: List of message dictionaries
            tools: List of tool schemas in OpenAI-compatible format
            **kwargs: Additional generation parameters
                - system: System prompt (CRITICAL for brain state instructions)
                - temperature: Temperature setting
                - max_tokens: Max completion tokens
                - metadata: Request metadata

        Returns:
            Either:
            - Tuple of (content, tool_calls, usage_data) when tool calls are present
              usage_data dict contains: prompt_tokens, completion_tokens, total_tokens
            - Just the content string when no tool calls

        Raises:
            LLMError: If generation fails
            NotImplementedError: If model doesn't support tool calling
        """
        try:
            self.logger.info(f"[generate_agent_response] Called with {len(messages)} messages and {len(tools) if tools else 0} tools")

            # Extract system message if present in kwargs
            system = kwargs.pop('system', None)
            temperature = kwargs.pop('temperature', None)
            max_tokens = kwargs.pop('max_tokens', None)

            # Check if this model supports tool calling (uses model_registry as source of truth)
            if not self._supports_tools():
                self.logger.warning(
                    f"OpenRouter model {self.model_type} does not support native tool calling. "
                    "Raising NotImplementedError to trigger fallback."
                )
                raise NotImplementedError(
                    f"OpenRouter model {self.model_type} does not support native tool/function calling"
                )

            # Call the tool-enabled generation method
            content, tool_calls, usage_data = await self._generate_with_tools(
                messages=messages,
                tools=tools,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )

            self.logger.info(f"[generate_agent_response] Returning {len(tool_calls)} tool calls + usage")

            # Always return the 3-tuple (Bug 2). Tool-free conversational/planning
            # turns (and every 0-recovered-call Kimi turn) previously returned a
            # bare content string, dropping message-level usage/billing telemetry.
            # tool_calls may be an empty list; OpenAI/Gemini clients already do this.
            return (content, tool_calls, usage_data)

        except NotImplementedError:
            # Re-raise NotImplementedError to trigger fallback
            raise
        except Exception as e:
            self.logger.error(f"Error in generate_agent_response: {e}", exc_info=True)
            raise LLMError(f"Failed to generate agent response: {e}")

    async def validate(self) -> bool:
        """Validate the OpenRouter client by making a test request."""
        try:
            if not self._client:
                await self._setup_client()

            response = await self._client.chat.completions.create(
                model=self.model_type,
                messages=[{"role": "user", "content": "test"}],
                max_tokens=1
            )
            if not response or not response.choices:
                raise ValueError(f"Invalid response from {self._PROVIDER_LABEL}")
            self.logger.info(f"{self._PROVIDER_LABEL} client validated with model {self.model_type}")
            return True
        except Exception as e:
            self.logger.error(f"{self._PROVIDER_LABEL} validation failed: {e}")
            # Use similar error translation as DeepSeek
            error_str = str(e).lower()
            if "rate" in error_str or "429" in error_str:
                raise ValueError(f"{self._PROVIDER_LABEL} API rate limit exceeded. Please try again later.")
            elif "key" in error_str or "auth" in error_str or "401" in error_str:
                raise ValueError(f"Invalid {self._PROVIDER_LABEL} API key.")
            else:
                raise ValueError(f"{self._PROVIDER_LABEL} validation failed: {str(e)}")

    async def _make_validation_request(self) -> Any:
        """Make a validation request to OpenRouter API.

        Required abstract method implementation for LLMClient base class.
        """
        return await self._client.chat.completions.create(
            model=self.model_type,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=1
        )

    def _check_validation_response(self, response: Any) -> None:
        """Check validation response from OpenRouter API.

        Required abstract method implementation for LLMClient base class.

        Args:
            response: Response from _make_validation_request

        Raises:
            LLMError: If response is invalid
        """
        if not response or not response.choices:
            raise LLMError(f"Invalid response from {self._PROVIDER_LABEL} API - no choices returned")

    def _extract_usage_data(self) -> Dict[str, Optional[int]]:
        """Extract usage data from response."""
        if not self.last_response or not hasattr(self.last_response, 'usage'):
            return {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None}

        usage = self.last_response.usage
        # UP-08: surface server-side prefix-cache hits (mirrors OpenAIClient). Benefits
        # NIM too (NvidiaClient subclasses this). cached_tokens feeds cached_input_price.
        cached_tokens = None
        details = getattr(usage, 'prompt_tokens_details', None)
        if details is not None:
            cached_tokens = getattr(details, 'cached_tokens', None)
        return {
            'prompt_tokens': getattr(usage, 'prompt_tokens', None),
            'completion_tokens': getattr(usage, 'completion_tokens', None),
            'total_tokens': getattr(usage, 'total_tokens', None),
            'cached_tokens': cached_tokens or 0,
        }

    async def cleanup(self) -> None:
        """Clean up resources."""
        await self._cleanup_client()
        self._initialized = False
        self.logger.info(f"{self._PROVIDER_LABEL} client cleaned up")
