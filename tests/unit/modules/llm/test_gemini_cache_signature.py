"""L1: Gemini explicit cachedContents were reused when only the tool-name signature
matched, but the cached object bakes in the system_instruction too. Two sessions with the
same tools but different system prompts (persona / SOUL / SELF / project-context) would
reuse the FIRST session's cached system prompt — a cross-session prompt leak. The reuse
key must include a hash of the system_instruction (098 MO-14: and the model).
"""
from modules.llm.gemini_cache import cache_key


def test_cache_signature_changes_with_system_prompt():
    assert cache_key("m", "persona A", []) != cache_key("m", "persona B", [])


def test_cache_signature_stable_for_same_inputs():
    assert cache_key("m", "same sys", []) == cache_key("m", "same sys", [])


def test_cache_signature_changes_when_system_prompt_none_vs_set():
    assert cache_key("m", None, []) != cache_key("m", "x", [])
