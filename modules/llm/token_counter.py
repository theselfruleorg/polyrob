"""
Token Counter Service for LLM Module

This module provides centralized token counting functionality for all models.
It handles encoder caching, model-specific counting, and telemetry.
"""

import logging
import threading
from typing import Optional, Dict, Any, Union, List
from dataclasses import dataclass
import json

# Optional imports for tokenizers
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False

# NOTE: the anthropic SDK is imported lazily at its only use site (Anthropic token
# encoder, below) so importing token_counter / count_tokens stays SDK-free. A top-level
# `from anthropic import Anthropic` dragged the SDK into the whole modules.llm /
# modules.memory import chain.

from modules.llm.model_registry import get_model_config, get_model_config_exact

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    """Token usage information"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int = 0
    # Cache-WRITE (creation) tokens, billed at a
    # provider surcharge (Anthropic 1.25x). Included in prompt_tokens; default 0.
    cache_creation_tokens: int = 0
    # The subset of cache_creation_tokens written with a 1h
    # window (Anthropic 2.0x). Default 0 = every write was a 5m write.
    cache_creation_1h_tokens: int = 0

    @property
    def billable_tokens(self) -> int:
        """Get billable token count (considering caching)"""
        return self.total_tokens - self.cached_tokens


class EncoderCache:
    """Thread-safe encoder cache with LRU eviction"""
    
    def __init__(self, max_size: int = 100):
        self._encoders = {}
        self._access_order = []
        self._lock = threading.RLock()
        self._max_size = max_size
    
    def get_encoder(self, model_name: str):
        """Get cached encoder for model with LRU eviction"""
        with self._lock:
            if model_name in self._encoders:
                # Move to end for LRU
                self._access_order.remove(model_name)
                self._access_order.append(model_name)
                return self._encoders[model_name]
            
            # Create new encoder
            encoder = self._create_encoder(model_name)
            
            # Add to cache with LRU eviction
            self._encoders[model_name] = encoder
            self._access_order.append(model_name)
            
            # Evict if needed
            while len(self._encoders) > self._max_size:
                oldest = self._access_order.pop(0)
                del self._encoders[oldest]
            
            return encoder
    
    def _create_encoder(self, model_name: str):
        """Create encoder for a specific model"""
        try:
            # OpenAI rows use tiktoken. An id with no row gets the character estimate, because
            # its first encoder load can fetch a file on the event loop.
            config = get_model_config_exact(model_name)
            if config is not None and config.provider.value == "openai" and TIKTOKEN_AVAILABLE:
                try:
                    return tiktoken.encoding_for_model(model_name)
                except KeyError:
                    # Modern OpenAI models (gpt-4o, gpt-4.1, gpt-5.x, o-series)
                    # use the o200k_base encoding, NOT cl100k_base.
                    # gpt-4/gpt-3.5 keep cl100k.
                    ml = model_name.lower()
                    if any(x in ml for x in ("gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4", "gpt-oss", "chatgpt-4o")):
                        return tiktoken.get_encoding("o200k_base")
                    return tiktoken.get_encoding("cl100k_base")

            # Anthropic has no usable synchronous tokenizer here (the SDK's
            # count_tokens is not usable on the installed version). Return
            # None so _count_text_tokens falls to the config.chars_per_token estimate.

            return None
            
        except Exception as e:
            logger.debug(f"Failed to create encoder for {model_name}: {e}")
            return None


class TokenCounter:
    """Centralized token counting service"""
    
    def __init__(self):
        self._encoder_cache = EncoderCache()
    
    def count_tokens(self, text: Union[str, List[Dict[str, Any]]], 
                    model_name: str) -> int:
        """Count tokens in text for a specific model"""
        if not text:
            return 0
        
        # Handle different content types
        if isinstance(text, list):
            return self._count_multimodal_tokens(text, model_name)
        else:
            return self._count_text_tokens(str(text), model_name)
    
    def _count_text_tokens(self, text: str, model_name: str, encode: bool = True) -> int:
        """Count tokens in plain text. ``encode=False`` is the character estimate only."""
        if not text:
            return 0

        config = get_model_config(model_name)
        encoder = self._encoder_cache.get_encoder(model_name) if encode else None

        if encoder:
            try:
                if hasattr(encoder, 'encode'):
                    return len(encoder.encode(text))
                elif hasattr(encoder, 'count_tokens'):
                    return encoder.count_tokens(text)
            except Exception as e:
                logger.debug(f"Encoder failed for {model_name}: {e}")
        
        # Fallback to character-based estimation using model config
        return int(len(text) / config.chars_per_token)
    
    def _count_multimodal_tokens(self, content: List[Dict[str, Any]],
                                model_name: str, encode: bool = True) -> int:
        """Count tokens in multimodal content (text + images)"""
        total_tokens = 0
        
        for item in content:
            if isinstance(item, dict):
                if 'text' in item:
                    total_tokens += self._count_text_tokens(item['text'], model_name, encode)
                elif 'image_url' in item:
                    # Images typically use ~800 tokens
                    total_tokens += 800
                elif 'image' in item:
                    total_tokens += 800
        
        return total_tokens
    
    def count_messages_tokens(self, messages: List[Dict[str, Any]],
                            model_name: str, encode: bool = True) -> int:
        """Count tokens in a list of messages (OpenAI format)"""
        total_tokens = 0
        
        # Message overhead varies by model
        message_overhead = 6 if get_model_config(model_name).provider.value == "anthropic" else 4
        
        for message in messages:
            # Add message overhead
            total_tokens += message_overhead
            
            # Count content tokens
            content = message.get('content', '')
            if isinstance(content, str):
                total_tokens += self._count_text_tokens(content, model_name, encode)
            elif isinstance(content, list):
                total_tokens += self._count_multimodal_tokens(content, model_name, encode)
            
            # Handle tool calls
            if 'tool_calls' in message:
                tool_calls_str = json.dumps(message['tool_calls'])
                total_tokens += self._count_text_tokens(tool_calls_str, model_name, encode)

            # Round-tripped reasoning is billed input.
            reasoning = message.get('reasoning_content')
            if isinstance(reasoning, str) and reasoning:
                total_tokens += self._count_text_tokens(reasoning, model_name, encode)

        return total_tokens


# Global instance
_token_counter = TokenCounter()


# Public API
def count_tokens(text: Union[str, List[Dict[str, Any]]], model_name: str) -> int:
    """Count tokens in text for a specific model"""
    return _token_counter.count_tokens(text, model_name)


def count_messages_tokens(messages: List[Dict[str, Any]], model_name: str, encode: bool = True) -> int:
    """Count tokens in a list of messages. ``encode=False`` loads no tokenizer."""
    return _token_counter.count_messages_tokens(messages, model_name, encode)


def count_tools_tokens(tools: Optional[List[Dict[str, Any]]]) -> int:
    """Estimate the input tokens of a request's tool schemas, chars/4 with no
    tokenizer: a cron rig sends about 190 KB of schemas on every step."""
    return len(json.dumps(tools, default=str)) // 4 if tools else 0
