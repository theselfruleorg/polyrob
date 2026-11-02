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
    asyncio.run(mp.build_prefetch_message("what did we pay on 2020-01-05", session_id="s",
                                          user_id="u", reader=dict(reader)))
    assert "max_age_days" not in seen and seen.get("drop_unverified") is True
    seen.clear()
    asyncio.run(mp.build_prefetch_message("We did not pay bro", session_id="s",
                                          user_id="u", reader=dict(reader)))
    assert seen.get("max_age_days") == 7


def test_an_unverified_line_inside_a_joined_row_is_dropped(provider):
    joined = ("[phase-summary:execution] Round two winners were judged and listed.\n"
              "[unverified] [phase-summary:payout] Six round two winners were paid.")
    _write(provider, joined, days=1)
    out = _recall(provider, "round two winners", max_age_days=7, drop_unverified=True)
    assert "judged and listed" in out
    assert "were paid" not in out


FACT = "The treasury wallet address is 0xabc0000000000000000000000000000000000001 on Base."


def test_a_durable_old_fact_is_still_recalled_by_the_owner_reader(provider):
    """Review M5: the 7-day cap is for run accounts, not for facts."""
    _write(provider, FACT, days=60)
    _write(provider, OLD, days=60)
    out = _recall(provider, "treasury wallet address bounty payout",
                  max_age_days=rr.OWNER_RECALL_MAX_AGE_DAYS, drop_unverified=True)
    assert "0xabc0000000000000000000000000000000000001" in out
    assert "33.33 USDC" not in out


def test_is_episodic_reads_the_phase_summary_marker():
    assert rr.is_episodic(OLD) and rr.is_episodic(GUESS)
    assert not rr.is_episodic(FACT)
