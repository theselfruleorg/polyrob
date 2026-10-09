"""A later upsert must not turn a value into another env assignment."""
import pytest
from core.env_file import env_write_error, upsert_env_var


@pytest.mark.parametrize("separator", ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\0"])
def test_env_rejects_all_line_separators(tmp_path, separator):
    value = "harmless" + separator + "INJECTED_FLAG=1"
    assert env_write_error("SAFE_FLAG", value)
    path = tmp_path / ".env"
    with pytest.raises(ValueError):
        upsert_env_var(path, "SAFE_FLAG", value)
    assert not path.exists()
