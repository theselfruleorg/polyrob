"""Tests for polyrob todos commands."""

import tempfile
from pathlib import Path
from click.testing import CliRunner


def test_todos_command_group_exists():
    """Test that the todos command group is registered."""
    from cli.polyrob import cli
    assert "todos" in cli.commands


def test_todos_list_no_file():
    """Test todos list when no todo.md exists."""
    from cli.commands.todos import todos
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(todos, ["list"])
        assert result.exit_code == 0
        assert "no todo file" in result.output.lower()


def test_todos_add_creates_file():
    """Test that todos add creates a todo.md file."""
    from cli.commands.todos import todos
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(todos, ["add", "Test todo item"])
        assert result.exit_code == 0
        assert Path("todo.md").exists()


def test_todos_list_shows_items():
    """Test that todos list shows added items."""
    from cli.commands.todos import todos
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(todos, ["add", "First todo"])
        runner.invoke(todos, ["add", "Second todo"])
        result = runner.invoke(todos, ["list"])
        assert "First todo" in result.output
        assert "Second todo" in result.output


def test_todos_done_marks_complete():
    """Test that todos done marks an item complete."""
    from cli.commands.todos import todos
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(todos, ["add", "Todo to complete"])
        result = runner.invoke(todos, ["done", "1"])
        assert result.exit_code == 0
        list_result = runner.invoke(todos, ["list"])
        assert "✓" in list_result.output or "completed" in list_result.output.lower()


def test_todos_stats_shows_progress():
    """Test that todos stats shows progress."""
    from cli.commands.todos import todos
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(todos, ["add", "Todo 1"])
        runner.invoke(todos, ["add", "Todo 2"])
        runner.invoke(todos, ["done", "1"])
        result = runner.invoke(todos, ["stats"])
        assert result.exit_code == 0
        assert "completed" in result.output.lower()


def test_todos_add_preserves_non_item_content(tmp_path):
    """add/done/clear must NOT drop headings/notes (the old regenerate-from-scratch
    format was lossy)."""
    from cli.commands.todos import todos
    f = tmp_path / "todo.md"
    f.write_text("# My Project\n\nNotes: keep me\n- [ ] existing\n")
    r = CliRunner().invoke(todos, ["add", "new task", "--file", str(f)])
    assert r.exit_code == 0, r.output
    content = f.read_text()
    assert "# My Project" in content
    assert "Notes: keep me" in content
    assert "- [ ] existing" in content
    assert "- [ ] new task" in content


def test_todos_done_preserves_non_item_content(tmp_path):
    from cli.commands.todos import todos
    f = tmp_path / "todo.md"
    f.write_text("# Header\n- [ ] a\n- [ ] b\nfootnote line\n")
    r = CliRunner().invoke(todos, ["done", "2", "--file", str(f)])
    assert r.exit_code == 0, r.output
    content = f.read_text()
    assert "# Header" in content
    assert "footnote line" in content
    assert "- [x] b" in content
    assert "- [ ] a" in content


def test_todos_clear_removes_completed():
    """Test that todos clear removes completed items."""
    from cli.commands.todos import todos
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(todos, ["add", "Todo 1"])
        runner.invoke(todos, ["add", "Todo 2"])
        runner.invoke(todos, ["done", "1"])
        result = runner.invoke(todos, ["clear"])
        assert result.exit_code == 0
        list_result = runner.invoke(todos, ["list"])
        # Should only show the incomplete item
        assert "Todo 2" in list_result.output
        assert "Todo 1" not in list_result.output or "completed" in list_result.output
