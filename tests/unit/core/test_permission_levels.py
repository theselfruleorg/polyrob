"""104 §4: a named level is a shorthand, and never moves money or compute."""
import pytest

from core import permission_levels as pl
from core.config_service import set_value


@pytest.fixture
def home(monkeypatch, tmp_path):
    for k in ("AUTONOMY_ENABLED", "AUTONOMY_MODE", "AUTONOMY_POSTURE", "APPROVAL_PROVIDER",
              "APPROVAL_REQUIRED_TOOLS", "POLYROB_LOCAL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "d"))
    (tmp_path / "h").mkdir(); (tmp_path / "d").mkdir()
    return tmp_path / "d"


def _env_lines(tmp_path):
    env = tmp_path / "h" / ".env"
    return env.read_text() if env.exists() else ""


@pytest.mark.parametrize("key", pl.WRITES)
def test_a_level_key_is_never_money_compute_builder_or_trust(key):
    upper = key.upper().replace(".", "_")
    for word in ("WALLET", "USD", "CAP", "DEFI", "X402", "PAYMENT", "BUDGET", "TRADE", "SPEND",
                 "COMPUTE", "BUILDER", "POLYROB_LOCAL", "AUTONOMY_MODE"):
        assert word not in upper


def test_each_level_writes_and_reads_back_at_the_terminal(home, monkeypatch, tmp_path):
    ok, _, _ = pl.apply_level("ask-first", surface="local", user_id="o1", home_dir=home)
    assert ok
    assert pl.current_level("o1", home) == "ask-first"

    ok, _, _ = pl.apply_level("standard", surface="local", user_id="o1", home_dir=home)
    assert ok
    assert pl.current_level("o1", home) == "standard"

    ok, _, _ = pl.apply_level("on-my-own", surface="local", user_id="o1", home_dir=home)
    assert ok
    env = _env_lines(tmp_path)
    assert "AUTONOMY_ENABLED=true" in env and "AUTONOMY_POSTURE=full" in env
    monkeypatch.setenv("AUTONOMY_ENABLED", "true")
    monkeypatch.setenv("AUTONOMY_POSTURE", "full")
    assert pl.current_level("o1", home) == "on-my-own"


def test_a_level_keeps_an_entry_the_person_added(home):
    set_value("approvals.require", "my_tool", user_id="o1", home_dir=home, confirm=True)
    pl.apply_level("ask-first", surface="local", user_id="o1", home_dir=home)
    assert "my_tool" in pl._pref_list("o1", home)
    pl.apply_level("standard", surface="local", user_id="o1", home_dir=home)
    assert pl._pref_list("o1", home) == ["my_tool"]
    # Under the auto provider the entry does not ask, so the level is what really happens.
    assert pl.current_level("o1", home) == "standard"


def test_a_wizard_preset_reads_as_ask_first(home, monkeypatch):
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", ",".join(pl._recommended()))
    monkeypatch.setenv("APPROVAL_PROVIDER", "interactive_cli")
    assert pl.current_level("o1", home) == "ask-first"


def test_a_chat_seat_queues_the_guarded_change(home):
    pl.apply_level("ask-first", surface="local", user_id="o1", home_dir=home)
    ok, lines, queued = pl.apply_level("standard", surface="telegram", user_id="o1", home_dir=home)
    assert ok and queued and any("queued" in line for line in lines)
    assert "waits for you in /pending" in pl.level_reply(
        "standard", surface="telegram", user_id="o1", home_dir=home)
    assert pl.current_level("o1", home) != "standard"


def test_an_unchanged_level_queues_nothing_on_a_chat_seat(home):
    _, _, queued = pl.apply_level("standard", surface="telegram", user_id="o1", home_dir=home)
    assert queued is False


def test_the_reply_says_what_it_wrote_and_when(home, monkeypatch):
    out = pl.level_reply("on-my-own", surface="local", user_id="o1", home_dir=home)
    assert out.startswith("Level on-my-own:")
    assert "AUTONOMY_POSTURE" in out and "takes effect" in out


def test_a_level_that_says_nothing_runs_takes_back_the_full_posture(home, monkeypatch, tmp_path):
    """`full` alone starts the cron ticker: with AUTONOMY_ENABLED off, jobs still ran."""
    pl.apply_level("on-my-own", surface="local", user_id="o1", home_dir=home)
    pl.apply_level("standard", surface="local", user_id="o1", home_dir=home)
    env = _env_lines(tmp_path)
    assert "AUTONOMY_ENABLED=false" in env and "AUTONOMY_POSTURE=silent" in env
    assert "AUTONOMY_POSTURE=full" not in env


def test_jobs_that_still_run_do_not_read_as_a_quiet_level(home, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ENABLED", "false")
    monkeypatch.setenv("AUTONOMY_POSTURE", "full")
    assert pl.current_level("o1", home) == "custom"
