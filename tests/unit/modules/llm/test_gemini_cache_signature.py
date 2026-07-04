"""L1: Gemini explicit cachedContents were reused when only the tool-name signature
matched, but the cached object bakes in the system_instruction too. Two sessions with the
same tools but different system prompts (persona / SOUL / SELF / project-context) would
reuse the FIRST session's cached system prompt — a cross-session prompt leak. The reuse
key must include a hash of the system_instruction.
"""
from modules.llm.gemini_client import GeminiClient


def test_cache_signature_changes_with_system_prompt():
    a = GeminiClient._cache_signature("persona A", [])
    b = GeminiClient._cache_signature("persona B", [])
    assert a != b


def test_cache_signature_stable_for_same_inputs():
    assert GeminiClient._cache_signature("same sys", []) == GeminiClient._cache_signature("same sys", [])


def test_cache_signature_changes_when_system_prompt_none_vs_set():
    assert GeminiClient._cache_signature(None, []) != GeminiClient._cache_signature("x", [])
