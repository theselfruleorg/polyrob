"""One stop enum for every provider. Pure, so the status views read it from core."""
from typing import Optional

#: Every provider's stop reason, lower case, to one of six values.
_STOP_REASONS = {
    "stop": "complete", "end_turn": "complete", "stop_sequence": "complete",
    "completed": "complete", "pause_turn": "complete",
    "length": "length", "max_tokens": "length", "model_length": "length",
    "max_output_tokens": "length", "model_context_window_exceeded": "length",
    "content_filter": "filtered", "safety": "filtered", "recitation": "filtered",
    "blocklist": "filtered", "prohibited_content": "filtered", "spii": "filtered",
    "image_safety": "filtered",
    "refusal": "refused",
    "error": "error", "failed": "error", "malformed_function_call": "error",
    "tool_calls": "tool_calls", "tool_use": "tool_calls", "function_call": "tool_calls",
}


def normalize_stop(reason: Optional[str]) -> Optional[str]:
    """``complete``, ``length``, ``filtered``, ``refused``, ``error`` or ``tool_calls`` for
    any provider's stop reason. None means "could not tell", never "it finished"."""
    return _STOP_REASONS.get(reason.strip().lower()) if isinstance(reason, str) else None
