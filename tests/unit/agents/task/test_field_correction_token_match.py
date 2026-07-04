"""B3 (high) — apply_action_field_corrections must not clobber unrelated fields.

The partial-match fallback used a raw substring test against short correction
keys, so e.g. 'quality' matched 'q'->'query' and 'context' matched 'text'->
'content', silently overwriting the real query/content field. Fixed to a
'_'-delimited token-boundary match with an anti-clobber guard.
"""
from agents.task.utils_json import apply_action_field_corrections as fix


def test_quality_field_does_not_clobber_query():
    out = fix("search_google", {"query": "cats", "quality": "high"})
    assert out["query"] == "cats"          # NOT overwritten by 'quality'
    assert out.get("quality") == "high"    # left as-is (no token match)


def test_context_field_does_not_become_content():
    out = fix("write_file", {"content": "the body", "context": "extra"})
    assert out["content"] == "the body"
    assert out.get("context") == "extra"   # 'text' is not a token of 'context'


def test_exact_correction_still_applies():
    # 'q' IS the field -> exact-key correction to 'query'.
    out = fix("search_google", {"q": "cats"})
    assert out["query"] == "cats"
    assert "q" not in out


def test_exact_write_file_corrections_still_apply():
    out = fix("write_file", {"file_name": "a.txt", "data": "hello"})
    assert out["file_path"] == "a.txt"
    assert out["content"] == "hello"


def test_token_boundary_positive_match():
    # 'name' is a whole token of 'user_name' and is a read_file correction key.
    out = fix("read_file", {"user_name": "a.txt"})
    assert out["file_path"] == "a.txt"
