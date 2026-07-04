from agents.task.agent.skill_frontmatter import (
    parse_frontmatter, strip_frontmatter, emit_frontmatter, parse_bool,
    encode_triggers, decode_triggers,
)

def test_parses_nested_metadata_block():
    content = (
        "---\n"
        "name: pdf-processing\n"
        "description: Extract PDF text. Use when handling PDFs.\n"
        "metadata:\n"
        "  polyrob-priority: \"3\"\n"
        "  polyrob-auto-activate: \"true\"\n"
        "  polyrob-triggers: '{\"keywords\":[\"pdf\"]}'\n"
        "---\n"
        "# PDF\nBody here.\n"
    )
    meta, body = parse_frontmatter(content)
    assert meta["name"] == "pdf-processing"
    assert meta["metadata"]["polyrob-priority"] == "3"          # nested, NOT flattened
    assert "polyrob-priority" not in meta                        # legacy flat-parser bug is gone
    assert body.startswith("# PDF")
    assert decode_triggers(meta["metadata"]["polyrob-triggers"]) == {"keywords": ["pdf"]}

def test_parse_bool_false_string_is_false():
    assert parse_bool("false") is False and parse_bool("0") is False
    assert parse_bool("") is False and parse_bool(None) is False
    assert parse_bool("true") is True and parse_bool("True") is True

def test_no_frontmatter_returns_empty_and_original_body():
    meta, body = parse_frontmatter("# Just a body\nhi")
    assert meta == {} and body == "# Just a body\nhi"
    # A leading BOM must be stripped from the returned body (no-frontmatter branch).
    _, bom_body = parse_frontmatter("﻿# Just a body\nhi")
    assert not bom_body.startswith("﻿") and bom_body == "# Just a body\nhi"

def test_emit_allowed_tools_list_roundtrips_as_space_string():
    text = emit_frontmatter({"name": "x", "allowed-tools": ["Bash", "Read"]})
    assert "allowed-tools: Bash Read" in text                     # space-joined, not a Python repr
    assert "['" not in text and "'Bash'" not in text              # no mangled list repr
    back, _ = parse_frontmatter(text + "\n# body")
    assert back["allowed-tools"] == "Bash Read"                   # reads back as a plain string

def test_emit_allowed_tools_string_passes_through():
    text = emit_frontmatter({"name": "x", "allowed-tools": "Bash Read"})
    back, _ = parse_frontmatter(text + "\n# body")
    assert back["allowed-tools"] == "Bash Read"                   # already a string -> unchanged

def test_emit_roundtrip_is_block_style_and_stringifies_metadata():
    meta = {"name": "x", "description": "d", "license": "MIT",
            "metadata": {"polyrob-priority": 3, "polyrob-triggers": encode_triggers({"keywords": ["a"]})}}
    text = emit_frontmatter(meta)
    assert text.startswith("---\n") and text.rstrip().endswith("---")
    assert "{" not in text.split("metadata:")[0]                # no flow style in top-level
    back, _ = parse_frontmatter(text + "\n# body")
    assert back["metadata"]["polyrob-priority"] == "3"          # coerced to string

def test_malformed_yaml_is_lenient_not_fatal():
    meta, body = parse_frontmatter("---\ndescription: a: b: c\n---\n# body")
    assert isinstance(meta, dict) and body.strip() == "# body"
