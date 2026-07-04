from core.interactive_gate import (
    interactive_turn, is_interactive_busy, mark_busy, mark_idle,
)


def test_idle_by_default():
    assert is_interactive_busy() is False


def test_context_manager_marks_busy_then_idle():
    assert is_interactive_busy() is False
    with interactive_turn():
        assert is_interactive_busy() is True
    assert is_interactive_busy() is False


def test_nested_turns_depth_counted():
    with interactive_turn():
        with interactive_turn():
            assert is_interactive_busy() is True
        assert is_interactive_busy() is True   # still busy until outer exits
    assert is_interactive_busy() is False


def test_mark_idle_never_goes_negative():
    mark_idle(); mark_idle()
    assert is_interactive_busy() is False
    mark_busy()
    assert is_interactive_busy() is True
    mark_idle()
    assert is_interactive_busy() is False
