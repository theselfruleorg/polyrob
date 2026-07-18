"""
Configuration for robust parse improvements in task agent.

This module contains all configurable settings for the parse robustness
improvements, making it easy to tune behavior without code changes.
"""

import os
from typing import Optional

from core.env import bool_env as _bool_env


def resolve_base64_strip_mode(raw: Optional[str]) -> dict:
    """Resolve a STRIP_BASE64_IMAGES value into its effective behaviour (A2).

    Returns ``{"strip_at_parse": bool, "anchor": bool}``.

      - ``"true"`` / anything unrecognized -> blunt parse-time strip (current default).
      - ``"false"``                        -> no stripping at all.
      - ``"anchor"``                       -> NO parse-time strip; rely on the
        anchor-preserving ``strip_historical_media`` pass (B3) to bound history while
        keeping the most-recent image-bearing turn for vision continuity.
    """
    v = (raw or "").strip().lower()
    if v == "anchor":
        return {"strip_at_parse": False, "anchor": True}
    if v == "false":
        return {"strip_at_parse": False, "anchor": False}
    return {"strip_at_parse": True, "anchor": False}


class RobustParseConfig:
    """Configuration for robust parse improvements"""

    # Feature flags
    ENABLE_ROBUST_PARSE: bool = _bool_env("ENABLE_ROBUST_PARSE", True)
    USE_LEGACY_DEEPSEEK: bool = _bool_env("USE_LEGACY_DEEPSEEK", False)

    # Content truncation settings - MINIMAL FIX (Nov 4, 2025): DISABLED
    # Note: Image token estimation is now handled by modules.llm.token_counter._count_multimodal_tokens
    # BEFORE: PAGE_CONTENT_TRUNCATE_LENGTH = 8000 (caused loops)
    # AFTER: 10M (effectively disabled) - let 1M context window handle it
    PAGE_CONTENT_TRUNCATE_LENGTH: int = int(os.getenv("PAGE_CONTENT_TRUNCATE_LENGTH", "10000000"))  # DISABLED - no truncation
    
    # CONTINUOUS CHAT: User guidance configuration
    # P0-1: the old 500-char per-message cut mangled pasted owner instructions AND
    # destroyed forged-turn (self-wake / delegation-result) payloads — those are
    # pre-wrapped (~700 chars of preamble+delimiters before any payload), so [:500]
    # delivered ZERO payload and left an UNCLOSED <untrusted_tool_result> tag in
    # history. Genuine messages now use head+tail middle-elision (keep the ask AND
    # its closing detail) at a defensible default; forged messages skip the per-
    # message cut entirely (they are bounded at their source, e.g. format_self_wake).
    MAX_USER_GUIDANCE_TOKENS: int = int(os.getenv("MAX_USER_GUIDANCE_TOKENS", "3000"))
    MAX_USER_MESSAGES_PER_STEP: int = int(os.getenv("MAX_USER_MESSAGES_PER_STEP", "3"))
    USER_MESSAGE_TRUNCATE_LENGTH: int = int(os.getenv("USER_MESSAGE_TRUNCATE_LENGTH", "4000"))
    USER_MESSAGE_KEEP_TAIL: int = int(os.getenv("USER_MESSAGE_KEEP_TAIL", "500"))
    # Ceiling for a forged (pre-wrapped) message body; large enough that a bounded
    # self-wake / delegation payload passes untouched, with its closing delimiter intact.
    FORGED_MESSAGE_MAX_CHARS: int = int(os.getenv("FORGED_MESSAGE_MAX_CHARS", "16000"))
    FORGED_MESSAGE_KEEP_TAIL: int = int(os.getenv("FORGED_MESSAGE_KEEP_TAIL", "3000"))
    
    # ActionResult content limits - Coordinated with tool limits
    # MAX_EXTRACTED_CONTENT_LENGTH: Target limit for tool outputs (browser, etc) - not currently enforced
    # MAX_EXTRACTED_CONTENT_SIZE: CRITICAL - content >this size offloaded to files
    # FIXED (Nov 6, 2025): Increased threshold after browser accessibility fix
    # Previous: 300K chars (too aggressive, triggered for legitimate large content from non-browser tools)
    # Production issue: Browser returned 1-2M char raw HTML → file offload → agent read back → loop
    # Solution: Browser now returns accessibility snapshots (30-80K chars), so higher threshold is safe
    # New: 500K chars = ~125K tokens = 12% of 1M context per message
    # Rationale: Browser won't trigger this anymore (returns clean content). For non-browser tools (filesystem, MCP),
    # file offload with smart preview is superior to keeping massive content in context
    MAX_EXTRACTED_CONTENT_LENGTH: int = int(os.getenv("MAX_EXTRACTED_CONTENT_LENGTH", "500000"))  # 500K chars - browser won't hit this
    MAX_EXTRACTED_CONTENT_SIZE: int = int(os.getenv("MAX_EXTRACTED_CONTENT_SIZE", "500000"))  # 500K chars - for non-browser large content
    LARGE_CONTENT_PREVIEW_LENGTH: int = int(os.getenv("LARGE_CONTENT_PREVIEW_LENGTH", "15000"))  # 15K preview - better context when offloaded
    
    # PHASE 2 FIX (Nov 4, 2025): Separate error truncation limits
    # Errors should be shorter (don't need full stack traces), successes can be longer
    MAX_ERROR_LENGTH: int = int(os.getenv("MAX_ERROR_LENGTH", "2000"))  # 2K for errors (up from 400)
    MAX_SUCCESS_LENGTH: int = int(os.getenv("MAX_SUCCESS_LENGTH", "100000"))  # 100K for successes (explicit limit)
    
    # Retry configuration - Balanced for reliability
    ENABLE_JSON_TEMPLATE_RETRY: bool = _bool_env("ENABLE_JSON_TEMPLATE_RETRY", True)
    MAX_PARSE_RETRIES: int = int(os.getenv("MAX_PARSE_RETRIES", "3"))  # Increased for reliability

    # Backoff configuration - Quick but reliable
    BASE_RETRY_DELAY: int = int(os.getenv("BASE_RETRY_DELAY", "1"))  # Quick first retry
    MAX_RETRY_DELAY: int = int(os.getenv("MAX_RETRY_DELAY", "5"))  # Capped at 5 seconds
    BACKOFF_MULTIPLIER: float = float(os.getenv("BACKOFF_MULTIPLIER", "1.5"))  # Moderate backoff
    
    # NEW: Context safety configuration - CRITICAL ADDITION
    ENABLE_CONTEXT_OVERFLOW_GUARD: bool = _bool_env("ENABLE_CONTEXT_OVERFLOW_GUARD", True)
    CONTEXT_OVERFLOW_THRESHOLD: float = float(os.getenv("CONTEXT_OVERFLOW_THRESHOLD", "0.90"))  # 90% of context window - maximize usage
    SAFETY_MARGIN_PERCENT: float = float(os.getenv("SAFETY_MARGIN_PERCENT", "0.05"))  # 5% safety margin - minimal to maximize context
    
    # NEW: Memory management optimization
    ENABLE_MEMORY_OPTIMIZATION: bool = _bool_env("ENABLE_MEMORY_OPTIMIZATION", True)
    MAX_MEMORY_CACHE_SIZE: int = int(os.getenv("MAX_MEMORY_CACHE_SIZE", "50"))  # Maximum cached messages
    MEMORY_CLEANUP_INTERVAL: int = int(os.getenv("MEMORY_CLEANUP_INTERVAL", "100"))  # Cleanup every N operations
    
    # NEW: Enhanced error handling configuration
    ENABLE_ENHANCED_ERROR_RECOVERY: bool = _bool_env("ENABLE_ENHANCED_ERROR_RECOVERY", True)
    MAX_CONSECUTIVE_FAILURES: int = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "5"))  # More lenient failure limit
    ERROR_RECOVERY_DELAY: float = float(os.getenv("ERROR_RECOVERY_DELAY", "2.0"))  # Delay before retry after error
    
    # JSON extraction - FIXED: Enhanced JSON extraction settings
    JSON_EXTRACT_LOG_LENGTH: int = int(os.getenv("JSON_EXTRACT_LOG_LENGTH", "1000"))
    PREFER_FIRST_JSON_MATCH: bool = _bool_env("PREFER_FIRST_JSON_MATCH", True)
    STRIP_CODE_FENCES: bool = True  # FIXED: Always strip code fences
    STRIP_THINK_TAGS: bool = True  # FIXED: Always strip think tags
    
    # NEW: JSON validation requirements
    REQUIRE_SCHEMA_KEYS: bool = _bool_env("REQUIRE_SCHEMA_KEYS", False)  # Disabled by default for flexibility
    REQUIRED_KEYS: list = ["current_state", "action"]  # Keys that must be present when validation enabled
    BRAIN_STATE_KEYS: list = ["memory", "evaluation_previous_goal", "next_goal", "reasoning"]  # For brain state validation in native tools mode
    
    # Function calling configuration
    FORCE_FUNCTION_CALLING_FOR_ALL: bool = _bool_env("FORCE_FUNCTION_CALLING", True)
    ENABLE_STRUCTURED_OUTPUT_FALLBACK: bool = _bool_env("ENABLE_STRUCTURED_OUTPUT_FALLBACK", True)  # NEW
    
    # Message cutting - Only cut when actually needed
    CUT_MESSAGES_BEFORE_EACH_LLM_CALL: bool = _bool_env("CUT_MESSAGES_BEFORE_LLM", False)  # Disabled by default
    TOKEN_BUFFER_SIZE: int = int(os.getenv("TOKEN_BUFFER_SIZE", "800"))  # FIXED: Increased buffer
    
    # NEW: Format hint management
    INJECT_FORMAT_HINT_EARLY: bool = _bool_env("INJECT_FORMAT_HINT_EARLY", True)
    MAX_FORMAT_HINT_TOKENS: int = int(os.getenv("MAX_FORMAT_HINT_TOKENS", "200"))  # NEW: Cap hint size
    
    # NEW: Recalibration settings
    FORCE_RECALIBRATE_AFTER_LARGE_ACTIONS: bool = _bool_env("FORCE_RECALIBRATE_AFTER_LARGE_ACTIONS", True)
    LARGE_ACTION_THRESHOLD: int = int(os.getenv("LARGE_ACTION_THRESHOLD", "500"))  # Chars that trigger recalibration
    
    # Evaluation/Planner isolation
    ISOLATE_EVALUATOR_MESSAGES: bool = _bool_env("ISOLATE_EVALUATOR_MESSAGES", True)
    EVALUATOR_MESSAGE_PREFIX: str = os.getenv("EVALUATOR_MESSAGE_PREFIX", "EVAL::")
    
    # File offloading - ENABLED for non-browser large content (Nov 6, 2025)
    # Browser tools now return accessibility snapshots (30-80K chars), won't trigger this
    # Useful for filesystem reads, MCP responses, and other tools returning large datasets
    STORE_LARGE_CONTENT_AS_FILES: bool = _bool_env("STORE_LARGE_CONTENT_AS_FILES", True)
    LARGE_CONTENT_FILE_PREFIX: str = os.getenv("LARGE_CONTENT_FILE_PREFIX", "large_content_")
    
    # NEW: Base64 image stripping (A2 — three modes: true | false | anchor)
    #   STRIP_BASE64_IMAGES drives the blunt parse-time strip (back-compat bool).
    #   STRIP_BASE64_ANCHOR_MODE selects the anchor-preserving path (parse-strip OFF;
    #   strip_historical_media keeps the latest image-bearing turn). Default 'true'.
    _B64_STRIP = resolve_base64_strip_mode(os.getenv("STRIP_BASE64_IMAGES", "true"))
    STRIP_BASE64_IMAGES: bool = _B64_STRIP["strip_at_parse"]
    STRIP_BASE64_ANCHOR_MODE: bool = _B64_STRIP["anchor"]
    
    # NEW: Telemetry deduplication
    ENABLE_TELEMETRY_DEDUPLICATION: bool = _bool_env("ENABLE_TELEMETRY_DEDUPLICATION", True)



    @classmethod
    def get_exponential_backoff_delay(cls, consecutive_failures: int) -> float:
        """Calculate exponential backoff delay with cap and jitter (P4: shared formula)."""
        from core.backoff import jittered_exponential_delay
        return jittered_exponential_delay(
            cls.BASE_RETRY_DELAY, consecutive_failures,
            multiplier=cls.BACKOFF_MULTIPLIER, cap=cls.MAX_RETRY_DELAY,
            cap_after_jitter=False,  # cap BEFORE jitter (may slightly exceed cap)
        )
    
    @classmethod
    def should_retry_parse_error(cls, consecutive_failures: int) -> bool:
        """Check if we should retry on parse error"""
        return (
            cls.ENABLE_ROBUST_PARSE and 
            cls.ENABLE_JSON_TEMPLATE_RETRY and 
            consecutive_failures < cls.MAX_PARSE_RETRIES
        )
    
    @classmethod
    def get_json_template_hint(cls) -> str:
        """Get the JSON template hint for retry - FIXED: More specific about action field requirements"""
        return """CRITICAL: Your response MUST be valid JSON with this exact structure:
{
  "current_state": {
    "page_summary": "Brief page summary",
    "evaluation_previous_goal": "Success/Failed/Unknown",
    "memory": "Key information to remember",
    "next_goal": "What to do next",
    "reasoning": "Why this action makes sense"
  },
  "action": [{"action_name": {"param": "value"}}]
}

CRITICAL ACTION FIELD REQUIREMENTS:
- For "done" action: {"done": {"text": "completion message"}} - USE "text", NOT "message"
- For "write_file" action: {"write_file": {"file_path": "path", "content": "text"}} - USE "file_path", NOT "file_name" or "path"
- For "click" action: {"click": {"selector": "element"}} - USE "selector"
- For "type" action: {"type": {"text": "input", "selector": "element"}} - USE "text" for content

Use double quotes only. No text outside JSON. Follow field names EXACTLY as shown."""
    
    @classmethod
    def should_store_content_as_file(cls, content: str) -> bool:
        """Check if content should be stored as file instead of in memory"""
        return (
            cls.STORE_LARGE_CONTENT_AS_FILES and
            len(content) > cls.MAX_EXTRACTED_CONTENT_LENGTH
        )
    
    @classmethod
    def get_model_max_tokens(cls, model_name: str) -> Optional[int]:
        """DEPRECATED: Use modules.llm.model_registry.get_model_config() instead.

        This method is deprecated as of v2.2.0.

        Args:
            model_name: Name of the model

        Returns:
            Max tokens or None for self-regulating models
        """
        # Delegate to centralized model registry
        from modules.llm.model_registry import get_model_config
        config = get_model_config(model_name)

        if config:
            # O-series reasoning models self-regulate
            model_lower = model_name.lower() if model_name else ""
            if any(m in model_lower for m in ["o1", "o3"]):
                return None  # Let reasoning models self-regulate

            return config.max_completion_tokens

        # Conservative default for unknown models
        return 8000
    
    @classmethod
    def truncate_page_content(cls, content: str) -> str:
        """MINIMAL FIX: No truncation - return full content.
        
        Args:
            content: The page content
            
        Returns:
            Full content (no truncation with 1M context available)
        """
        if not content:
            return content
        
        # Strip base64 images (can be massive and useless)
        if cls.STRIP_BASE64_IMAGES:
            content = cls.strip_base64_images(content)
        
        # NO TRUNCATION - return full content
        return content
    
    @classmethod
    def truncate_extracted_content(cls, content: str) -> str:
        """MINIMAL FIX: No truncation - return full content.
        
        This is only called as fallback when file offloading fails.
        With 500K offload threshold, this rarely happens.
        When it does, return full content (we have 1M token context).
        """
        # NO TRUNCATION - return full content
        return content
    
    @classmethod
    def strip_base64_images(cls, content: str) -> str:
        """Strip base64 image data URLs from content"""
        if not cls.STRIP_BASE64_IMAGES:
            return content
            
        import re
        # Remove data:image URLs which can be very large
        content = re.sub(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', '[IMAGE_REMOVED]', content)
        return content
    
    @classmethod
    def validate_json_candidate(cls, candidate: str) -> bool:
        """Validate if a JSON candidate contains required schema keys and is well-formed.
        
        Args:
            candidate: JSON string to validate
            
        Returns:
            True if candidate is valid, False otherwise
        """
        if not cls.REQUIRE_SCHEMA_KEYS:
            return True
            
        try:
            # FIXED: More thorough JSON validation
            import json
            
            # First check if it's valid JSON
            parsed = json.loads(candidate.strip())
            
            # Must be a dictionary
            if not isinstance(parsed, dict):
                return False
            
            # FIXED: Check for all required keys with proper nesting
            for key in cls.REQUIRED_KEYS:
                if key not in parsed:
                    return False
                    
                # Additional validation for specific keys
                if key == "current_state":
                    # current_state should be a dict with specific fields
                    current_state = parsed[key]
                    if not isinstance(current_state, dict):
                        return False
                    # Check for essential current_state fields
                    required_state_fields = ["page_summary", "evaluation_previous_goal", "memory", "next_goal"]
                    if not any(field in current_state for field in required_state_fields):
                        return False
                        
                elif key == "action":
                    # action should be a list
                    actions = parsed[key]
                    if not isinstance(actions, list):
                        return False
                    # Should have at least one action (empty action list is usually invalid)
                    if len(actions) == 0:
                        return False
                    # Each action should be a dict with at least one key
                    for action in actions:
                        if not isinstance(action, dict) or len(action) == 0:
                            return False
            
            return True
            
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return False
    
    @classmethod
    def estimate_context_usage(cls, estimated_tokens: int, model_name: str) -> float:
        """Estimate what percentage of model context is being used"""
        # Use centralized model configuration
        # Use centralized model registry
        try:
            from modules.llm.model_registry import get_model_config
            model_config = get_model_config(model_name)
            if model_config and model_config.context_window and model_config.context_window > 0:
                return estimated_tokens / model_config.context_window
        except ImportError:
            pass
        
        # Final fallback - use reasonable defaults based on model name patterns
        model_name_lower = model_name.lower() if model_name else ""
        
        # GPT models
        if "gpt-5" in model_name_lower:
            default_context = 1047576  # 1M tokens for gpt-5 series
        elif "gpt-4.5" in model_name_lower:
            default_context = 128000
        elif "gpt-4-turbo" in model_name_lower:
            default_context = 128000
        elif "gpt-4" in model_name_lower:
            default_context = 8192
        elif "gpt-3.5" in model_name_lower:
            default_context = 16385
        # Claude models  
        elif any(m in model_name_lower for m in ["claude-3", "claude-4"]):
            default_context = 200000  # Most Claude 3/4 models have 200k context
        # Gemini models
        elif "gemini-2.5" in model_name_lower:
            default_context = 2097152  # 2M tokens for Gemini 2.5 Pro
        elif any(m in model_name_lower for m in ["gemini-1.5", "gemini-2.0"]):
            default_context = 1048576  # 1M tokens for Gemini 1.5/2.0 Flash
        elif "gemini" in model_name_lower:
            default_context = 32768  # Other Gemini models
        # DeepSeek models
        elif "deepseek-chat" in model_name_lower:
            default_context = 64000  # DeepSeek Chat has 64k context
        elif "deepseek-reasoner" in model_name_lower:
            default_context = 64000  # DeepSeek Reasoner
        elif "deepseek" in model_name_lower:
            default_context = 16000  # Other DeepSeek models
        # Llama models
        elif "llama" in model_name_lower:
            default_context = 128000  # Modern Llama models typically have large context
        # O-series models
        elif any(m in model_name_lower for m in ["o1", "o3"]):
            default_context = 200000  # O-series reasoning models
        else:
            # Conservative default for unknown models
            default_context = 8192
        
        return estimated_tokens / default_context
    
    @classmethod
    def should_abort_context_overflow(cls, estimated_tokens: int, model_name: str) -> bool:
        """Check if we should abort due to context overflow"""
        if not cls.ENABLE_CONTEXT_OVERFLOW_GUARD:
            return False
            
        usage_ratio = cls.estimate_context_usage(estimated_tokens, model_name)
        return usage_ratio > cls.CONTEXT_OVERFLOW_THRESHOLD


# Export configuration instance
config = RobustParseConfig() 