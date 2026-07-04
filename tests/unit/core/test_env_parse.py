import pytest
from core.env import parse_bool


@pytest.mark.parametrize("val", ["off", "false", "0", "no", "none", "", "  OFF  "])
def test_falsey_values_return_false_when_default_true(val):
    assert parse_bool(val, True) is False


@pytest.mark.parametrize("val", ["on", "true", "1", "yes", "anything"])
def test_truthy_values_return_true_when_default_false(val):
    assert parse_bool(val, False) is True


def test_none_returns_default():
    assert parse_bool(None, True) is True
    assert parse_bool(None, False) is False
