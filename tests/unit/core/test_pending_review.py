"""core.pending_review — what the owner reads before deciding a pending proposal."""
from core.pending_review import (REVIEW_MAX_CHARS, review_text, skill_change,
                                 split_frontmatter, summary_line)

LIVE = "---\nname: x\ndescription: Old way\n---\nstep one\nstep two\n"
DRAFT = "---\nname: x\ndescription: Better way\n---\nstep one\nstep 2b\nstep three\n"


def test_frontmatter_split():
    meta, rest = split_frontmatter(DRAFT)
    assert meta["description"] == "Better way"
    assert rest.startswith("step one")


def test_new_skill_change():
    c = skill_change(DRAFT, None)
    assert c["new"] is True and c["removed"] == 0
    assert c["description"] == "Better way"


def test_update_counts_lines():
    c = skill_change(DRAFT, LIVE)
    assert c["new"] is False
    assert (c["added"], c["removed"]) == (3, 2)  # description + two body lines


def test_summary_line_uses_description_not_frontmatter():
    item = {"change": skill_change(DRAFT, LIVE), "preview": "---\nname: x"}
    line = summary_line(item)
    # 096 W2: an update names what CHANGED, not the new description.
    assert line.startswith('update — description changed: "Old way" → "Better way"'), line
    assert "name:" not in line


def test_summary_line_keeps_note_whole():
    item = {"notes": "drafted by the agent", "preview": "[drafted by the agent] " + "a" * 500}
    line = summary_line(item)
    assert line.startswith("[drafted by the agent] a")
    assert line.count("[drafted by the agent]") == 1
    assert line.endswith("…")


def test_review_update_is_a_diff():
    out = review_text({"kind": "skill", "id": "x"}, DRAFT, label="skill 'x'",
                      decide_line="Decide: /approve_p_1 /reject_p_1",
                      is_skill=True, active_body=LIVE)
    assert "```diff" in out and "+step three" in out and "-step two" in out
    assert out.endswith("Decide: /approve_p_1 /reject_p_1")


def test_review_new_item_whole_and_fence_safe():
    body = "text with ``` inside"
    out = review_text({"kind": "skill", "id": "x"}, body, label="skill 'x'",
                      decide_line="d", is_skill=True)
    assert "a new skill" in out
    assert out.count("```") == 2  # the inner fence is neutralised


def test_review_caps_long_body():
    out = review_text({"kind": "owner", "id": "1"}, "z" * (REVIEW_MAX_CHARS + 50),
                      label="owner-facts note", decide_line="d", is_skill=False)
    assert "50 more characters not shown" in out
    assert "polyrob owner show-pending owner 1" in out
    assert "polyrob owner pending show" not in out
    # 096 A6: the note sits AFTER the closing fence, never inside it.
    assert out.index("more characters not shown") > out.rindex("```")
