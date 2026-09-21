"""POLYROB todos — a standalone workspace todo.md editor.

This manages a plain Markdown checkbox file (default ``./todo.md``, override with
``--file``). It is NOT wired to the agent's live per-session todos — those are
session-scoped (the task tool writes them under the session tree); view them in the
REPL with ``/todos``. Use this command to keep a simple project checklist.

Edits are content-preserving: add/done/clear only touch the ``- [ ]`` / ``- [x]``
lines and leave headings, notes, and blank lines intact.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import List

import click

TODO_MD_FILE = "todo.md"

#: C50: the two todo lists are different objects with different lifetimes, and
#: nothing on this seat said so — an operator who ran `polyrob todos list` after
#: a session and saw nothing concluded the agent had lost its list.
_REPL_TWIN = ("(the agent's LIVE per-session todos are a different list — "
              "see `/todos` in the REPL)")


def _no_file(todo_file: str) -> str:
    from cli.ui.candy import empty
    return (empty(f"todo file at {todo_file}",
                  "create one:  polyrob todos add \"…\"", yet=False)
            + "\n  " + _REPL_TWIN)

# A Markdown checkbox item line, with capture groups for in-place rewriting:
#   group(1)=prefix "- ["  group(2)=" "|"x"  group(3)="] "  group(4)=text
_ITEM_RE = re.compile(r'^(\s*-\s*\[)([ xX])(\]\s*)(.+)$')


def _parse_todo_md(content: str) -> List[dict]:
    """Parse todo.md content into a list of checkbox items (read-only view)."""
    items = []
    for line in content.splitlines():
        match = _ITEM_RE.match(line.strip())
        if match:
            _, status, _, text = match.groups()
            items.append({
                "completed": status.lower() == "x",
                "text": text.strip(),
            })
    return items


def _read_lines(path: Path) -> List[str]:
    return path.read_text().splitlines() if path.exists() else []


def _write_lines(path: Path, lines: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _item_line_positions(lines: List[str]) -> List[int]:
    """Line indices (in order) that are checkbox items — the mapping from a 1-based
    todo index to its physical line, so non-item lines are preserved on edit."""
    return [i for i, ln in enumerate(lines) if _ITEM_RE.match(ln.strip())]


@click.group("todos")
def todos():
    """Manage a standalone workspace todo.md (not the agent's live session todos)."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


@todos.command("list")
@click.option("--file", "todo_file", default=TODO_MD_FILE, help="Todo file path.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def todos_list(todo_file: str, as_json: bool):
    """List todos from the workspace todo file."""
    path = Path(todo_file)
    if not path.exists():
        if as_json:
            click.echo("[]")
        else:
            click.echo(_no_file(todo_file))
        return

    content = path.read_text()
    items = _parse_todo_md(content)

    if as_json:
        click.echo(json.dumps(items, indent=2))
    else:
        if not items:
            from cli.ui.candy import empty
            click.echo(empty(f"todos in {todo_file}",
                             "add one:  polyrob todos add \"…\""))
            click.echo(click.style(_REPL_TWIN, dim=True))
            return

        completed = sum(1 for i in items if i["completed"])
        total = len(items)
        click.echo(f"Todos in {todo_file}: {completed}/{total} completed")
        click.echo("-" * 40)
        for item in items:
            marker = click.style("✓", fg="green") if item["completed"] else click.style("○", fg="yellow")
            click.echo(f"  {marker} {item['text']}")


@todos.command("add")
@click.argument("text")
@click.option("--file", "todo_file", default=TODO_MD_FILE, help="Todo file path.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def todos_add(text: str, todo_file: str, as_json: bool):
    """Add a new todo item (appended; existing content preserved)."""
    path = Path(todo_file)
    lines = _read_lines(path)
    lines.append(f"- [ ] {text}")
    _write_lines(path, lines)
    total = len(_item_line_positions(lines))

    if as_json:
        click.echo(json.dumps({"added": text, "total": total}, indent=2))
    else:
        click.echo(click.style("[polyrob] ", fg="green") + f"Added: {text}")


@todos.command("done")
@click.argument("index", type=int)
@click.option("--file", "todo_file", default=TODO_MD_FILE, help="Todo file path.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def todos_done(index: int, todo_file: str, as_json: bool):
    """Mark a todo as complete (1-based index)."""
    path = Path(todo_file)
    if not path.exists():
        if as_json:
            click.echo(json.dumps({"error": f"no todo file at {todo_file}"}, indent=2))
        else:
            click.echo(click.style("[polyrob] ERROR: ", fg="red") + _no_file(todo_file))
        sys.exit(1)

    lines = _read_lines(path)
    positions = _item_line_positions(lines)
    if index < 1 or index > len(positions):
        msg = f"invalid index {index} (have {len(positions)} items)"
        if as_json:
            click.echo(json.dumps({"error": msg}, indent=2))
        else:
            click.echo(click.style("[polyrob] ERROR: ", fg="red") + msg)
        sys.exit(1)

    li = positions[index - 1]
    prefix, status, mid, text = _ITEM_RE.match(lines[li]).groups()
    if status.lower() == "x":
        if as_json:
            click.echo(json.dumps({"completed": text.strip(), "index": index,
                                   "changed": False}, indent=2))
        else:
            click.echo(click.style("[polyrob] ", fg="yellow") + "Already completed")
        return

    lines[li] = f"{prefix}x{mid}{text}"  # flip in place; surrounding content untouched
    _write_lines(path, lines)

    if as_json:
        click.echo(json.dumps({"completed": text.strip(), "index": index}, indent=2))
    else:
        click.echo(click.style("[polyrob] ", fg="green") + f"Completed: {text.strip()}")


@todos.command("clear")
@click.option("--file", "todo_file", default=TODO_MD_FILE, help="Todo file path.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def todos_clear(todo_file: str, as_json: bool):
    """Clear completed todos (non-item lines preserved)."""
    path = Path(todo_file)
    if not path.exists():
        if as_json:
            click.echo(json.dumps({"cleared": 0, "remaining": 0,
                                   "error": f"no todo file at {todo_file}"}, indent=2))
        else:
            click.echo(_no_file(todo_file))
        return

    lines = _read_lines(path)
    kept: List[str] = []
    cleared = 0
    for ln in lines:
        m = _ITEM_RE.match(ln.strip())
        if m and m.group(2).lower() == "x":
            cleared += 1
            continue
        kept.append(ln)

    if cleared == 0:
        remaining = len(_item_line_positions(lines))
        if as_json:
            click.echo(json.dumps({"cleared": 0, "remaining": remaining}, indent=2))
        else:
            from cli.ui.candy import empty
            click.echo(empty("completed todos to clear",
                             f"{remaining} item(s) are still open", yet=False))
        return

    _write_lines(path, kept)
    remaining = len(_item_line_positions(kept))

    if as_json:
        click.echo(json.dumps({"cleared": cleared, "remaining": remaining}, indent=2))
    else:
        click.echo(click.style("[polyrob] ", fg="green") +
                   f"Cleared {cleared} completed todo(s)")


@todos.command("stats")
@click.option("--file", "todo_file", default=TODO_MD_FILE, help="Todo file path.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def todos_stats(todo_file: str, as_json: bool):
    """Show todo statistics."""
    path = Path(todo_file)
    if not path.exists():
        if as_json:
            click.echo(json.dumps({"total": 0, "completed": 0, "remaining": 0,
                                   "percent_complete": 0.0,
                                   "error": f"no todo file at {todo_file}"}, indent=2))
        else:
            click.echo(_no_file(todo_file))
        return

    items = _parse_todo_md(path.read_text())
    completed = sum(1 for i in items if i["completed"])
    total = len(items)
    percent = (completed / total * 100) if total > 0 else 0

    stats = {
        "total": total,
        "completed": completed,
        "remaining": total - completed,
        "percent_complete": round(percent, 1),
    }

    if as_json:
        click.echo(json.dumps(stats, indent=2))
    else:
        click.echo(f"Todo Stats ({todo_file}):")
        click.echo(f"  Total: {stats['total']}")
        click.echo(f"  Completed: {stats['completed']}")
        click.echo(f"  Remaining: {stats['remaining']}")
        click.echo(f"  Progress: {stats['percent_complete']}%")
