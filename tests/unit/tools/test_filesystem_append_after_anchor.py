"""Appending relative to an anchor, so a newest-first table can gain a row.

⚠️ The defect this closes, measured in production on 2026-09-23. The SAFETY rail
keeps a `## History of the metrics that matter` table in a 2,718-line file, rows
newest-first at the TOP. Its newest row was **02:22**: the 04:21, 06:21, 08:21
and 10:22 runs all failed to add theirs. The rail said why, at step 9 of 9:

    "The filesystem toolset has no in-place patch primitive, so the correct move
     is to append the new row via a targeted write… the row goes at the TOP of
     the table, so I must rewrite the—"

…and then it ran out of steps. That reasoning was CORRECT. The `money_rail` rig
grants `("defi_data", "defi_trade", "filesystem", "task", "message")`, and
`filesystem` offered append-at-end and whole-file write with nothing in between.
Rewriting 2,718 lines to prepend one row is not a reasonable move.

**It reached a money decision.** At 10:49 the PNL buyback computed its rate
reference from that table and reported: *"That is 5 samples in the window (fewer
than 6)"* — three of the missing samples were the rows the monitor could not
write. The tranche still passed its checks, but a money rail sized a $218.50
trade against a degraded reference because a text file could not gain a line.

So `append_file` takes an optional `after_anchor`: insert immediately AFTER the
first line containing that exact text. Two refusals are deliberate and both
matter more than the feature:

* an anchor that matches NOTHING refuses — silently appending at the end would
  bury a history row 2,700 lines below where anyone reads it, which is the same
  failure wearing a success message;
* an anchor that matches MORE THAN ONCE refuses and says how many — guessing
  which of several tables to write into is not a thing a money rail should do.
"""
import asyncio

import pytest

from tools.controller.views import AppendFileAction


TABLE = """# Safety monitor

## History of the metrics that matter

| Timestamp | Rate |
|---|---|
| 2026-09-23 02:22 | 707,215,000 |
| 2026-09-22 22:21 | 72,777,783.99 |

## Notes
older prose
"""

SEPARATOR = "|---|---|"


def _append(tool, path, content, anchor=None):
    kwargs = {"file_path": path, "content": content}
    if anchor is not None:
        kwargs["after_anchor"] = anchor
    # asyncio.run, not get_event_loop(): an earlier test that closes/clears the
    # loop left none current and this file failed only in the full shard.
    return asyncio.run(tool.append_file(AppendFileAction(**kwargs)))


@pytest.fixture
def tool(tmp_path, monkeypatch):
    from tools.filesystem import FileSystem as FilesystemTool
    t = FilesystemTool.__new__(FilesystemTool)
    t._enabled = True
    t.session_id = None
    t.user_id = "rob"
    t.workspace_dir = str(tmp_path)
    t.logger = __import__("logging").getLogger("test.fs")
    t.name = "filesystem"

    async def _ready():
        return None

    t.ensure_initialized = _ready
    t._normalize_path = lambda p: str(tmp_path / p)
    t._guard_path = lambda np, orig, write=False: str(tmp_path)
    t._safe_read_text = lambda p, root: open(p, encoding="utf-8").read()
    t._content_receipt = lambda c: {"size_bytes": len(c.encode()), "sha256": "x" * 16}
    return t


# --- the model ----------------------------------------------------------------- #

def test_the_field_is_optional_so_every_existing_call_is_unchanged():
    a = AppendFileAction(file_path="f.md", content="x")
    assert a.after_anchor is None


def test_a_blank_anchor_is_absent_not_an_empty_match():
    """'' is in every line. Treating it as an anchor would insert at line 1."""
    assert AppendFileAction(file_path="f.md", content="x", after_anchor="   ").after_anchor is None


# --- inserting ----------------------------------------------------------------- #

def test_a_row_lands_directly_under_the_anchor(tool, tmp_path):
    f = tmp_path / "baseline.md"
    f.write_text(TABLE, encoding="utf-8")
    _append(tool, "baseline.md", "| 2026-09-23 10:22 | 86,169,757 |", anchor=SEPARATOR)

    lines = f.read_text(encoding="utf-8").splitlines()
    i = lines.index(SEPARATOR)
    assert lines[i + 1] == "| 2026-09-23 10:22 | 86,169,757 |"
    assert lines[i + 2] == "| 2026-09-23 02:22 | 707,215,000 |"   # pushed down, not replaced


def test_nothing_else_in_the_file_moves(tool, tmp_path):
    f = tmp_path / "baseline.md"
    f.write_text(TABLE, encoding="utf-8")
    _append(tool, "baseline.md", "| new |", anchor=SEPARATOR)
    after = f.read_text(encoding="utf-8")
    assert after.startswith("# Safety monitor\n")
    assert after.rstrip().endswith("older prose")
    assert "| 2026-09-22 22:21 | 72,777,783.99 |" in after


def test_the_anchor_matches_a_substring_of_its_line(tool, tmp_path):
    f = tmp_path / "baseline.md"
    f.write_text(TABLE, encoding="utf-8")
    _append(tool, "baseline.md", "| new |", anchor="History of the metrics")
    lines = f.read_text(encoding="utf-8").splitlines()
    assert lines[lines.index("## History of the metrics that matter") + 1] == "| new |"


def test_multi_line_content_keeps_its_order(tool, tmp_path):
    f = tmp_path / "baseline.md"
    f.write_text(TABLE, encoding="utf-8")
    _append(tool, "baseline.md", "| a |\n| b |", anchor=SEPARATOR)
    lines = f.read_text(encoding="utf-8").splitlines()
    i = lines.index(SEPARATOR)
    assert lines[i + 1:i + 3] == ["| a |", "| b |"]


# --- the refusals, which are the point ----------------------------------------- #

def test_an_absent_anchor_REFUSES_rather_than_appending_at_the_end(tool, tmp_path):
    """The whole defect in one test: a silent fallback would report success and
    bury the row at the bottom of a 2,718-line file."""
    from core.exceptions import ServiceError
    f = tmp_path / "baseline.md"
    f.write_text(TABLE, encoding="utf-8")
    with pytest.raises(ServiceError) as e:
        _append(tool, "baseline.md", "| new |", anchor="no such line")
    assert "no such line" in str(e.value)
    assert f.read_text(encoding="utf-8") == TABLE        # untouched


def test_an_ambiguous_anchor_REFUSES_and_says_how_many(tool, tmp_path):
    from core.exceptions import ServiceError
    f = tmp_path / "two.md"
    f.write_text("| a |\n---\n| b |\n---\n", encoding="utf-8")
    with pytest.raises(ServiceError) as e:
        _append(tool, "two.md", "| new |", anchor="---")
    assert "2" in str(e.value)
    assert f.read_text(encoding="utf-8") == "| a |\n---\n| b |\n---\n"


def test_an_anchor_on_a_file_that_does_not_exist_REFUSES(tool, tmp_path):
    """Anchored insert into nothing is a mistake, not a create — the caller
    believed there was a table to write into."""
    from core.exceptions import ServiceError
    with pytest.raises(ServiceError):
        _append(tool, "absent.md", "| new |", anchor=SEPARATOR)
    assert not (tmp_path / "absent.md").exists()


# --- plain append is untouched -------------------------------------------------- #

def test_without_an_anchor_it_still_appends_at_the_end(tool, tmp_path):
    f = tmp_path / "log.md"
    f.write_text("first\n", encoding="utf-8")
    _append(tool, "log.md", "second")
    assert f.read_text(encoding="utf-8") == "first\nsecond"


def test_without_an_anchor_it_still_creates_a_missing_file(tool, tmp_path):
    _append(tool, "new.md", "hello")
    assert (tmp_path / "new.md").read_text(encoding="utf-8") == "hello"
