"""``metadata.polyrob-requires`` parses to a clean id list for every shape a
SKILL.md can carry (release review 1.4.x): a JSON string '"a"' used to split
into characters, a YAML list into "['a'" fragments, and 123 / null raised
TypeError out of the rule loader."""
import pytest

from core.skills.catalog import rule_from_frontmatter


def _req(value):
    return rule_from_frontmatter({"metadata": {"polyrob-requires": value}}).get("requires")


@pytest.mark.parametrize("value,expected", [
    ('["a","b"]', ["a", "b"]),
    ("a, b", ["a", "b"]),
    ('"secret-handling"', ["secret-handling"]),
    (["a", "b"], ["a", "b"]),
    ("123", []),
    ("null", []),
    ('{"a": 1}', []),
])
def test_requires_shapes(value, expected):
    assert _req(value) == expected
