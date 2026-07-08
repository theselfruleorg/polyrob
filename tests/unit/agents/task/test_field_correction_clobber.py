"""P0-3 — apply_action_field_corrections must not clobber a legitimate param
when the model supplies BOTH the right field and a synonym.

The direct-lookup path (`correct_field = action_corrections.get(field_lower, field)`)
renamed the synonym onto the target key with no clobber guard, so e.g.
done({'text': 'REAL COMPLETION', 'summary': 'short'}) destroyed the real text.
The fuzzy-token branch already had the guard; this locks the same guard onto the
direct path, order-independently: a genuine field always wins regardless of dict
ordering, and the redundant synonym is dropped.
"""
from agents.task.utils_json import apply_action_field_corrections as fix


# --- The three repro'd corruptions: genuine field first ---

def test_done_text_survives_summary_synonym():
    out = fix("done", {"text": "REAL COMPLETION", "summary": "short"})
    assert out["text"] == "REAL COMPLETION"
    assert "summary" not in out  # redundant synonym dropped, not renamed


def test_write_file_content_survives_data_synonym():
    out = fix("write_file", {"file_path": "a.txt", "content": "REAL", "data": "OTHER"})
    assert out["file_path"] == "a.txt"
    assert out["content"] == "REAL"
    assert "data" not in out


def test_anysite_search_query_survives_keyword_synonym():
    out = fix("anysite_search", {"keyword": "x", "query": "real query"})
    assert out["query"] == "real query"
    assert "keyword" not in out


# --- Same cases with reversed dict ordering (synonym first) ---

def test_done_synonym_first_genuine_still_wins():
    out = fix("done", {"summary": "short", "text": "REAL COMPLETION"})
    assert out["text"] == "REAL COMPLETION"
    assert "summary" not in out


def test_write_file_synonym_first_genuine_still_wins():
    out = fix("write_file", {"data": "OTHER", "content": "REAL", "file_path": "a.txt"})
    assert out["content"] == "REAL"
    assert out["file_path"] == "a.txt"
    assert "data" not in out


def test_anysite_search_synonym_first_genuine_still_wins():
    out = fix("anysite_search", {"query": "real query", "keyword": "x"})
    assert out["query"] == "real query"
    assert "keyword" not in out


# --- Legacy rename behavior preserved when the target is absent ---

def test_done_message_still_renames_to_text():
    assert fix("done", {"message": "hi"}) == {"text": "hi"}


def test_write_file_file_name_still_renames_to_file_path():
    out = fix("write_file", {"file_name": "a.txt"})
    assert out["file_path"] == "a.txt"
    assert "file_name" not in out


def test_write_file_both_renames_apply_when_targets_absent():
    out = fix("write_file", {"file_name": "a.txt", "data": "hello"})
    assert out == {"file_path": "a.txt", "content": "hello"}
