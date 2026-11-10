"""F7 (prod review 2026-10-29): owner-chat recall answered state questions with
10-14 rows and with false ``[unverified]`` phase summaries.

Replay of prod recall: "We did not pay bro" -> two 10-14 rows about USDC cards;
"pay in PNL not USDG" -> the false "dollar value fixed" rows. An owner chat now
recalls only rows from the last ``OWNER_RECALL_MAX_AGE_DAYS`` days unless the
owner names an older date, and never an ``[unverified]`` row.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from core.sqlite_util import execute_retry
from modules.memory import recall_rank as rr
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

DAY = 86400


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"), top_k=5)


def _write(provider, content, *, days=0.0, sid="s-old"):
    asyncio.run(provider.sync_turn("q", content, session_id=sid, user_id="u"))
    execute_retry(provider.db_path,
                  "UPDATE mem_provenance SET ts = ? WHERE mem_rowid = "
                  "(SELECT rowid FROM memories WHERE content = ?)",
                  (int(time.time() - days * DAY), content))


OLD = "[phase-summary:payout] Bounty payout cards were 33.33 USDC on Base for each winner."
NEW = "[phase-summary:payout] Bounty payout cards for round two pay PNL tokens to each winner."
GUESS = ("[unverified] [phase-summary:execution] Bounty payout cards quote the live PNL "
         "price so the dollar value of each winner card stays fixed.")


def _recall(provider, query, **reader):
    return asyncio.run(provider.prefetch(query, session_id="s-now", user_id="u", **reader))


def test_without_the_owner_reader_every_row_is_recalled(provider):
    for c, d in ((OLD, 15), (NEW, 1), (GUESS, 1)):
        _write(provider, c, days=d)
    out = _recall(provider, "bounty payout cards winner")
    assert "33.33 USDC" in out and "round two" in out and "dollar value" in out


def test_the_owner_reader_drops_old_and_unverified_rows(provider):
    for c, d in ((OLD, 15), (NEW, 1), (GUESS, 1)):
        _write(provider, c, days=d)
    out = _recall(provider, "bounty payout cards winner",
                  max_age_days=rr.OWNER_RECALL_MAX_AGE_DAYS, drop_unverified=True)
    assert "round two" in out
    assert "33.33 USDC" not in out
    assert "dollar value" not in out


def test_the_owner_reader_is_forwarded_by_the_registry(provider, monkeypatch):
    from modules.memory import registry as R
    for c, d in ((OLD, 15), (NEW, 1)):
        _write(provider, c, days=d)
    monkeypatch.setattr(R, "get_memory_registry",
                        lambda: SimpleNamespace(active=lambda: provider))
    out = asyncio.run(R.memory_prefetch("bounty payout cards winner", session_id="s-now",
                                        user_id="u", max_age_days=7, drop_unverified=True))
    assert "round two" in out and "33.33" not in out


@pytest.mark.parametrize("query,older", [
    ("what did we pay on 2026-10-14?", True),
    ("what did we decide on 10-14", True),
    ("the round from Oct 14 — was it paid?", True),
    ("what did we decide on 14 October", True),
    ("We did not pay bro", False),
    ("pay in PNL not USDG", False),
    # review M5: a fraction, a range or a month-like word is no date
    ("send 1/2 of the balance", False),
    ("post 3-4 tweets today", False),
    ("post 3 junk tweets", False),
    ("2 marketing posts", False),
    ("what happened since 2/30", False),           # no such day
    ("what did we pay since 10/14", True),
])
def test_a_query_that_names_an_older_date_lifts_the_cap(query, older):
    now = time.mktime((2026, 10, 29, 12, 0, 0, 0, 0, -1))
    assert rr.names_older_date(query, max_age_days=7, now=now) is older


def test_a_recent_date_does_not_lift_the_cap():
    now = time.mktime((2026, 10, 29, 12, 0, 0, 0, 0, -1))
    assert rr.names_older_date("what happened on 2026-10-28?", max_age_days=7, now=now) is False


def test_the_owner_chat_reader_carries_the_cap_and_a_cron_reader_does_not():
    from agents.task.agent.core.memory_prefetch import recall_reader
    from core.autonomy_marker import mark_autonomous
    owner = recall_reader(SimpleNamespace(session_id="rr-chat", orchestrator=SimpleNamespace()))
    assert owner["max_age_days"] == rr.OWNER_RECALL_MAX_AGE_DAYS
    assert owner["drop_unverified"] is True
    mark_autonomous("rr-cron", None, cron_job_id="job-rr")
    cron = recall_reader(SimpleNamespace(session_id="rr-cron", orchestrator=SimpleNamespace()))
    assert "max_age_days" not in cron and "drop_unverified" not in cron


def test_build_prefetch_lifts_the_cap_for_an_older_date(monkeypatch):
    from agents.task.agent.core import memory_prefetch as mp
    seen = {}

    async def fake_prefetch(query, **kw):
        seen.update(kw)
        return ""

    monkeypatch.setattr("modules.memory.registry.memory_prefetch", fake_prefetch)
    reader = {"demote_automation": True, "max_age_days": 7, "drop_unverified": True}
    asking = "what did we pay on 2020-01-05"
    asyncio.run(mp.build_prefetch_message(asking, session_id="s", user_id="u",
                                          reader=dict(reader), owner_text=asking))
    assert "max_age_days" not in seen and seen.get("drop_unverified") is True
    seen.clear()
    asyncio.run(mp.build_prefetch_message("We did not pay bro", session_id="s", user_id="u",
                                          reader=dict(reader), owner_text="We did not pay bro"))
    assert seen.get("max_age_days") == 7


def test_a_date_the_owner_did_not_write_does_not_lift_the_cap(monkeypatch):
    """The query also holds the brain's next_goal and a carried subject."""
    from agents.task.agent.core import memory_prefetch as mp
    seen = {}

    async def fake_prefetch(query, **kw):
        seen.update(kw)
        return ""

    monkeypatch.setattr("modules.memory.registry.memory_prefetch", fake_prefetch)
    reader = {"max_age_days": 7}
    query = "yes do it Check the report from 2020-01-05"
    asyncio.run(mp.build_prefetch_message(query, session_id="s", user_id="u",
                                          reader=dict(reader), owner_text="yes do it"))
    assert seen.get("max_age_days") == 7
    seen.clear()
    asyncio.run(mp.build_prefetch_message(query, session_id="s", user_id="u", reader=dict(reader)))
    assert seen.get("max_age_days") == 7


def test_the_step_passes_the_owner_message_as_owner_text(monkeypatch):
    from agents.task.agent.core import memory_prefetch as mp
    seen = {}

    async def fake_build(query, **kw):
        seen.update(kw, query=query)
        return None

    monkeypatch.setattr(mp, "build_prefetch_message", fake_build)
    agent = mp.MemoryPrefetchMixin()
    agent.session_id = "rr-owner-text"
    agent.task = "the first message of the chat"
    agent.state = SimpleNamespace(n_steps=1)
    agent.orchestrator = SimpleNamespace()
    agent.logger = SimpleNamespace(debug=lambda *a, **k: None)
    asyncio.run(agent._maybe_prefetch_memory())
    assert seen["owner_text"] == "the first message of the chat"
    seen.clear()
    agent._recall_turn_text = "what happened on 2020-01-05"
    agent._last_brain_state = {"next_goal": "read the 2019-02-03 file"}
    asyncio.run(agent._maybe_prefetch_memory())
    assert seen["owner_text"] == "what happened on 2020-01-05"


def test_an_unverified_line_inside_a_joined_row_is_dropped(provider):
    joined = ("[phase-summary:execution] Round two winners were judged and listed.\n"
              "[unverified] [phase-summary:payout] Six round two winners were paid.")
    _write(provider, joined, days=1)
    out = _recall(provider, "round two winners", max_age_days=7, drop_unverified=True)
    assert "judged and listed" in out
    assert "were paid" not in out


FACT = "The treasury wallet address is 0xabc0000000000000000000000000000000000001 on Base."
NARRATION = "Read the treasury wallet ledger and appended 40 bytes to the bounty payout log."


def _set_kind(provider, content, kind):
    execute_retry(provider.db_path,
                  "UPDATE mem_provenance SET kind = ? WHERE mem_rowid = "
                  "(SELECT rowid FROM memories WHERE content = ?)", (kind, content))


def test_a_row_of_a_durable_kind_has_no_age_limit(provider):
    """The 7-day cap is for run accounts, not for facts."""
    _write(provider, FACT, days=60)
    _set_kind(provider, FACT, rr.DURABLE_KINDS[0])
    _write(provider, OLD, days=60)
    out = _recall(provider, "treasury wallet address bounty payout",
                  max_age_days=rr.OWNER_RECALL_MAX_AGE_DAYS, drop_unverified=True)
    assert "0xabc0000000000000000000000000000000000001" in out
    assert "33.33 USDC" not in out


def test_an_old_row_with_no_mark_is_episodic(provider):
    """9,212 of 9,375 prod rows have no mark. Old narration was read as a fact."""
    _write(provider, NARRATION, days=60)
    query = "treasury wallet ledger bounty payout"
    assert _recall(provider, query, max_age_days=rr.OWNER_RECALL_MAX_AGE_DAYS) == ""
    assert "appended 40 bytes" in _recall(provider, query)


def test_a_recent_row_with_no_mark_is_recalled(provider):
    _write(provider, NARRATION, days=1)
    assert "appended 40 bytes" in _recall(provider, "treasury wallet ledger",
                                          max_age_days=rr.OWNER_RECALL_MAX_AGE_DAYS)


def test_no_row_is_deleted_by_the_age_rule(provider):
    """D5: no purge. The explicit search still reads the old row."""
    _write(provider, NARRATION, days=60)
    assert "appended 40 bytes" in asyncio.run(
        provider.search("treasury wallet ledger", user_id="u"))


def test_the_vector_half_applies_the_same_age_rule(tmp_path, monkeypatch):
    from modules.memory.local_vector_memory_provider import LocalVectorMemoryProvider
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    prov = LocalVectorMemoryProvider(str(tmp_path / "m.db"))
    prov._vec_ok = False
    _write(prov, NARRATION, days=60)
    _write(prov, FACT, days=60)
    _set_kind(prov, FACT, rr.DURABLE_KINDS[0])
    prov._vec_ok = True
    monkeypatch.setattr(prov, "_vector_contents", lambda *a, **k: [NARRATION, FACT])
    # the query shares no keyword, so both rows are vector-only hits
    out = _recall(prov, "zzqq", max_age_days=rr.OWNER_RECALL_MAX_AGE_DAYS)
    assert "0xabc0000000000000000000000000000000000001" in out
    assert "appended 40 bytes" not in out


# --- One unverified rule, per line, at every site ------------------------------

def test_strip_reads_the_mark_at_any_place_in_a_line():
    row = "Checked the ledger. [unverified] The swaps stopped on Monday.\nThe ledger is clean."
    assert rr.strip_unverified(row) == "The ledger is clean."
    assert rr.strip_unverified("[unverified] a\n[unverified] b") == ""


def test_strip_judges_a_transcript_row_on_its_findings():
    q = "User: did we pay the winners"
    assert rr.strip_unverified(f"{q}\nAssistant: [unverified] [phase-summary:x] paid") == ""
    assert rr.strip_unverified(f"{q}\nAssistant: [unverified] a\n[phase-summary:y] b") == (
        f"{q}\nAssistant: [phase-summary:y] b")
    assert rr.strip_unverified(f"{q}\nAssistant: fine") == f"{q}\nAssistant: fine"


def test_mark_unverified_marks_each_line_once():
    assert rr.mark_unverified("a\n\n[unverified] b\nc") == "[unverified] a\n\n[unverified] b\n[unverified] c"


def test_a_multi_line_unverified_summary_does_not_leak(provider):
    row = ("[unverified] [phase-summary:payout] Six round two winners were paid.\n"
           "The payout cards were sent to round two winners.")
    assert rr.strip_unverified(rr.mark_unverified(row)) == ""
    _write(provider, rr.mark_unverified(row), days=1)
    assert _recall(provider, "round two winners", max_age_days=7, drop_unverified=True) == ""


def test_a_verified_line_after_an_unverified_first_line_stays(provider):
    joined = ("[unverified] [phase-summary:payout] Six round two winners were paid.\n"
              "[phase-summary:execution] Round two winners were judged and listed.")
    _write(provider, joined, days=1)
    out = _recall(provider, "round two winners", max_age_days=7, drop_unverified=True)
    assert "judged and listed" in out and "were paid" not in out


def test_a_mid_line_mark_is_dropped_in_recall(provider):
    _write(provider, "[phase-summary:x] Round two started. [unverified] Round two winners were paid.",
           days=1)
    assert _recall(provider, "round two winners", max_age_days=7, drop_unverified=True) == ""


def test_the_vector_half_applies_the_line_rule(tmp_path, monkeypatch):
    from modules.memory.local_vector_memory_provider import LocalVectorMemoryProvider
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    prov = LocalVectorMemoryProvider(str(tmp_path / "m.db"))
    prov._vec_ok = False
    joined = "Checked the zzqq ledger.\n[unverified] The zzqq winners were paid."
    guess = "Listed items. [unverified] All zzqq items shipped."
    _write(prov, joined, days=1)
    _write(prov, guess, days=1)
    prov._vec_ok = True
    monkeypatch.setattr(prov, "_vector_contents", lambda *a, **k: [joined, guess])
    out = _recall(prov, "zzqq winners", max_age_days=7, drop_unverified=True)
    assert out.count("Checked the zzqq ledger") == 1
    assert "paid" not in out and "shipped" not in out


def test_a_server_default_row_is_filtered_and_keeps_its_findings(tmp_path, monkeypatch):
    """The answer-only flag is off on a plain server: the mark sits after ``Assistant:``."""
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "false")
    prov = SqliteMemoryProvider(str(tmp_path / "m.db"))
    finding = "[unverified] [phase-summary:x] The zzqq winners were paid."
    asyncio.run(prov.sync_turn("zzqq " * 2000, finding, session_id="s-old", user_id="u"))
    row = execute_retry(prov.db_path, "SELECT content FROM memories", fetch="all")[0]["content"]
    assert len(row) <= 4000 and row.endswith(finding), "a long task must not push the findings out"
    assert _recall(prov, "zzqq winners", drop_unverified=True) == ""
    assert "were paid" in _recall(prov, "zzqq winners")


def test_a_short_server_default_row_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "false")
    assert SqliteMemoryProvider._compose_stored_content("q", "a") == "User: q\nAssistant: a"


# --- Explicit search: the unverified rule and the age, no age limit -------------

def _search(provider, query, **kw):
    return asyncio.run(provider.search(query, user_id="u", **kw))


def test_an_explicit_search_applies_the_unverified_rule_when_the_reader_asks(provider):
    _write(provider, "[phase-summary:x] Checked the zzqq ledger.\n[unverified] The zzqq winners were paid.")
    assert "paid" in _search(provider, "zzqq winners")
    out = _search(provider, "zzqq winners", drop_unverified=True)
    assert "paid" not in out and "Checked the zzqq ledger" in out


def test_an_explicit_search_keeps_old_rows_and_shows_their_age(provider):
    _write(provider, "The zzqq ledger closed.", days=60)
    line = _search(provider, "zzqq ledger", with_age=True)
    assert "60d old] The zzqq ledger closed." in line
    assert "d old" not in _search(provider, "zzqq ledger")


def test_a_row_with_no_stamp_says_its_age_is_unknown(provider):
    execute_retry(provider.db_path,
                  "INSERT INTO memories (user_id, session_id, content) VALUES (?,?,?)",
                  ("u", "old", "legacy zzqq row"))
    assert _search(provider, "zzqq", with_age=True) == "- [age unknown] legacy zzqq row"


def test_the_owner_reader_reaches_the_search_without_its_age_cap(provider, monkeypatch):
    from modules.memory import registry as R
    monkeypatch.setattr(R, "get_memory_registry",
                        lambda: SimpleNamespace(active=lambda: provider))
    _write(provider, "The zzqq ledger closed.", days=60)
    _write(provider, "[unverified] The zzqq winners were paid.", days=1)
    out = asyncio.run(R.memory_search("zzqq", user_id="u", with_age=True, max_age_days=7,
                                      drop_unverified=True))
    assert "60d old] The zzqq ledger closed." in out and "paid" not in out


def test_the_vector_search_applies_both(tmp_path, monkeypatch):
    from modules.memory.local_vector_memory_provider import LocalVectorMemoryProvider
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    prov = LocalVectorMemoryProvider(str(tmp_path / "m.db"))
    prov._vec_ok = False
    old = "The aabb ledger closed."
    guess = "Listed items.\n[unverified] All aabb items shipped."
    _write(prov, old, days=60)
    _write(prov, guess, days=1)
    prov._vec_ok = True
    monkeypatch.setattr(prov, "_vector_contents", lambda *a, **k: [old, guess])
    out = _search(prov, "zzqq", drop_unverified=True, with_age=True)
    assert "60d old] The aabb ledger closed." in out
    assert "shipped" not in out and "Listed items." in out
