"""A state part goes out again only when a fact changed, not when a clock moved."""
from agents.task.agent.message_manager.state_facts import StateFact, state_facts, state_hash

NOTE = ("Live health as of 2026-11-06 12:16 UTC:\n"
        "- delivery: owner messages 24h: {n}/300 cap used, 0 suppressed\n"
        "- wallet 0xabc (read {age} ago)\n  robinhood: {eth} ETH")
JOB = ('- j1 "Daily report" · 0 9 * * * · next {next} UTC · last: {last} · deliver: telegram')
SPEND = "- rolling 24h cap: $50.00 (used ${used}, left ${left})"


def test_a_clock_or_a_counter_does_not_change_the_hash():
    first = NOTE.format(n=13, age="26s", eth="8.5")
    second = NOTE.format(n=14, age="1m", eth="8.5").replace("12:16", "12:18")
    assert state_hash(first) == state_hash(second)


def test_a_balance_changes_the_hash():
    assert (state_hash(NOTE.format(n=13, age="26s", eth="8.5"))
            != state_hash(NOTE.format(n=13, age="26s", eth="7.0")))


def test_a_suppressed_message_or_a_new_cap_changes_the_hash():
    base = "- delivery: owner messages 24h: 3/300 cap used, 0 suppressed"
    assert state_hash(base) != state_hash(base.replace("0 suppressed", "2 suppressed"))
    assert state_hash(base) != state_hash(base.replace("/300", "/500"))


def test_the_next_run_time_and_the_last_run_time_are_not_facts():
    a = JOB.format(next="2026-11-07 09:00", last="done 11-06 09:00")
    b = JOB.format(next="2026-11-08 09:00", last="done 11-07 09:00")
    assert state_hash(a) == state_hash(b)


def test_a_failed_run_and_a_running_job_are_facts():
    done = JOB.format(next="2026-11-07 09:00", last="done 11-06 09:00")
    failed = JOB.format(next="2026-11-07 09:00", last="failed 11-06 09:00 (step limit)")
    running = done.replace("next 2026-11-07 09:00 UTC", "running now")
    assert len({state_hash(done), state_hash(failed), state_hash(running)}) == 3


def test_spend_inside_a_cap_is_not_a_fact_and_the_cap_is():
    a, b = SPEND.format(used="1.00", left="49.00"), SPEND.format(used="9.00", left="41.00")
    assert state_hash(a) == state_hash(b)
    assert state_hash(a) != state_hash(a.replace("$50.00", "$80.00"))
    assert state_facts(a) == (StateFact("spend_cap", "$50.00"),)


def test_an_unknown_line_is_one_whole_fact():
    assert state_facts("- something new: 7") == (StateFact("line", "- something new: 7"),)
    assert state_hash("- something new: 7") != state_hash("- something new: 8")


def test_the_render_header_is_no_fact():
    assert state_facts("Live harness state as of 2026-11-06 12:00 UTC (same source as /status):") == ()
