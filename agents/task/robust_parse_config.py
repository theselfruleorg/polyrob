"""
Configuration for robust parse improvements in task agent.

This module contains all configurable settings for the parse robustness
improvements, making it easy to tune behavior without code changes.
"""

import os
from typing import Optional

from core.env import bool_env as _bool_env, int_env as _int_env


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
    # (Dead reads deleted 2026-08-27, 030 WS-F2: USE_LEGACY_DEEPSEEK and
    # PAGE_CONTENT_TRUNCATE_LENGTH had no consumer anywhere in the tree.)

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
    # History: 300K chars (too aggressive) -> 500K chars (Nov 6, 2025, after the browser
    # accessibility fix stopped returning 1-2M char raw HTML).
    #
    # F26 (2026-09-22 harness/cache review): 500K chars is ~125K TOKENS — a threshold the
    # disk-pointer path almost never reached, so a single grep/web_fetch/MCP answer of 90K
    # tokens rode in the context instead. A reference agent offloads at 100K chars per result and 200K
    # chars per TURN; DeepSeek Harness and Pi at 50 KB. Both numbers below are that pair:
    #   MAX_EXTRACTED_CONTENT_SIZE      — per RESULT (~25K tokens)
    #   MAX_EXTRACTED_CONTENT_TURN_SIZE — per TURN, summed over the step's results; over it,
    #                                     the LARGEST results go to disk first until the turn
    #                                     fits (three 80K results each pass the per-result
    #                                     test and together are 240K chars of context).
    # 0 disables either check. The offloaded content is never lost — it is a workspace file
    # the model reads back with read_file, named in the pointer.
    MAX_EXTRACTED_CONTENT_LENGTH: int = int(os.getenv("MAX_EXTRACTED_CONTENT_LENGTH", "500000"))  # 500K chars - browser won't hit this
    MAX_EXTRACTED_CONTENT_SIZE: int = _int_env("MAX_EXTRACTED_CONTENT_SIZE", 100000)  # 100K chars ~ 25K tokens, per result
    MAX_EXTRACTED_CONTENT_TURN_SIZE: int = _int_env("MAX_EXTRACTED_CONTENT_TURN_SIZE", 200000)  # 200K chars ~ 50K tokens, per turn
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
    # F19: the denominator of last resort. Only reached when there is no live
    # MessageManager AND the model registry does not know the id — a registry gap.
    # Deliberately small so the guard fires early rather than late.
    UNKNOWN_MODEL_CONTEXT_WINDOW: int = 8192
    SAFETY_MARGIN_PERCENT: float = float(os.getenv("SAFETY_MARGIN_PERCENT", "0.05"))  # 5% safety margin - minimal to maximize context

    # (Dead reads deleted 2026-08-27, 030 WS-F2 — no consumer anywhere in the
    # tree: ENABLE_MEMORY_OPTIMIZATION, ENABLE_ENHANCED_ERROR_RECOVERY,
    # ERROR_RECOVERY_DELAY, JSON_EXTRACT_LOG_LENGTH, PREFER_FIRST_JSON_MATCH,
    # ENABLE_STRUCTURED_OUTPUT_FALLBACK, TOKEN_BUFFER_SIZE,
    # MAX_FORMAT_HINT_TOKENS, ISOLATE_EVALUATOR_MESSAGES,
    # EVALUATOR_MESSAGE_PREFIX, LARGE_CONTENT_FILE_PREFIX,
    # ENABLE_TELEMETRY_DEDUPLICATION; the MAX_MEMORY_CACHE_SIZE /
    # MEMORY_CLEANUP_INTERVAL / MAX_CONSECUTIVE_FAILURES duplicates — the live
    # MAX_MEMORY_CACHE_SIZE/MEMORY_CLEANUP_INTERVAL reads are in
    # agents/task/constants.py.)

    STRIP_CODE_FENCES: bool = True  # FIXED: Always strip code fences
    STRIP_THINK_TAGS: bool = True  # FIXED: Always strip think tags

    # NEW: JSON validation requirements
    REQUIRE_SCHEMA_KEYS: bool = _bool_env("REQUIRE_SCHEMA_KEYS", False)  # Disabled by default for flexibility
    REQUIRED_KEYS: list = ["current_state", "action"]  # Keys that must be present when validation enabled

    # NEW: Format hint management
    INJECT_FORMAT_HINT_EARLY: bool = _bool_env("INJECT_FORMAT_HINT_EARLY", True)

    # NEW: Recalibration settings
    FORCE_RECALIBRATE_AFTER_LARGE_ACTIONS: bool = _bool_env("FORCE_RECALIBRATE_AFTER_LARGE_ACTIONS", True)
    LARGE_ACTION_THRESHOLD: int = int(os.getenv("LARGE_ACTION_THRESHOLD", "500"))  # Chars that trigger recalibration

    # File offloading - ENABLED for non-browser large content (Nov 6, 2025)
    # NOTE (030 WS-F2): the LIVE offload path is result_offload.py, keyed on
    # MAX_EXTRACTED_CONTENT_SIZE + LARGE_CONTENT_PREVIEW_LENGTH. The pair below
    # only feeds should_store_content_as_file (pinned by
    # tests/test_accessibility_snapshot.py; no production caller today).
    STORE_LARGE_CONTENT_AS_FILES: bool = _bool_env("STORE_LARGE_CONTENT_AS_FILES", True)

    # NEW: Base64 image stripping — blunt parse-time strip; "false" disables,
    # anything else strips. (The unconsumed "anchor" mode attr was removed.)
    _B64_STRIP = resolve_base64_strip_mode(os.getenv("STRIP_BASE64_IMAGES", "true"))
    STRIP_BASE64_IMAGES: bool = _B64_STRIP["strip_at_parse"]

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
- For "filesystem_write_file": {"filesystem_write_file": {"file_path": "path", "content": "text"}} - USE "file_path", NOT "file_name" or "path"
- For "browser_click_element": {"browser_click_element": {"index": 3}} - USE the element "index" from the page state
- For "browser_input_text": {"browser_input_text": {"index": 3, "text": "input"}} - USE "index" and "text"
- Use only action names from your tool list; these are examples of the shape

Use double quotes only. No text outside JSON. Follow field names EXACTLY as shown."""
    
    @classmethod
    def should_store_content_as_file(cls, content: str) -> bool:
        """Check if content should be stored as file instead of in memory"""
        return (
            cls.STORE_LARGE_CONTENT_AS_FILES and
            len(content) > cls.MAX_EXTRACTED_CONTENT_LENGTH
        )
    
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
    def strip_base64_images(cls, content: str) -> str:
        """Strip base64 image data URLs from content"""
        if not cls.STRIP_BASE64_IMAGES:
            return content
            
        import re
        # Remove data:image URLs which can be very large. M14: the subtype is
        # bounded — the old `[^;]+` rescanned to end-of-input from every
        # `data:image/` on untrusted content with no `;` (quadratic).
        content = re.sub(r'data:image/[^;\s]{1,40};base64,[A-Za-z0-9+/=]+', '[IMAGE_REMOVED]', content)
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
    def estimate_context_usage(cls, estimated_tokens: int, model_name: str,
                               message_manager=None) -> float:
        """What fraction of the usable input budget ``estimated_tokens`` is.

        F19 — ONE denominator. This used to divide by a raw model context window
        (and, when the registry did not know the model, by a hardcoded ladder of
        per-model constants that had to be edited for every new id), while the
        MessageManager divided by ``max_input_tokens`` (``0.95*cw - reserve``,
        plus any ``TASK_MAX_INPUT_TOKENS`` operator cap). The owner could see two
        different "how full is it" numbers for the same request.

        Pass the live ``message_manager`` and its ``context_usage()["limit"]`` IS
        the denominator. Without one (a pure call from a test or a tool with no
        session) the model registry's window is the fallback, and an unknown
        model falls back to a single conservative default rather than a ladder —
        an unknown id is a registry gap to fix, not a table to grow.
        """
        if estimated_tokens <= 0:
            return 0.0

        limit = 0
        if message_manager is not None:
            try:
                limit = int(message_manager.context_usage()["limit"] or 0)
            except Exception:
                limit = int(getattr(message_manager, "max_input_tokens", 0) or 0)
        if limit <= 0:
            try:
                from modules.llm.model_registry import get_model_config
                model_config = get_model_config(model_name)
                if model_config and model_config.context_window:
                    limit = int(model_config.context_window)
            except ImportError:
                limit = 0
        if limit <= 0:
            # Conservative: a model nobody can size is treated as small, so the
            # guard fires EARLY rather than passing a request the provider drops.
            limit = cls.UNKNOWN_MODEL_CONTEXT_WINDOW

        return estimated_tokens / limit

    @classmethod
    def should_abort_context_overflow(cls, estimated_tokens: int, model_name: str,
                                      message_manager=None) -> bool:
        """Check if we should abort due to context overflow"""
        if not cls.ENABLE_CONTEXT_OVERFLOW_GUARD:
            return False

        usage_ratio = cls.estimate_context_usage(estimated_tokens, model_name,
                                                 message_manager=message_manager)
        return usage_ratio > cls.CONTEXT_OVERFLOW_THRESHOLD


# Export configuration instance
config = RobustParseConfig() 