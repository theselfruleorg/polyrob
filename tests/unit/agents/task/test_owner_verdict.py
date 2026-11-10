"""108 W5: an owner reply bound to a rail reaches that rail's latest episode."""
import asyncio
import json
import sqlite3
import types

import pytest

from agents.task.agent.core import episodic_digest as ED
from agents.task.agent.core import owner_verdict as OV
from core.surfaces import owner_thread as ot
from modules.memory.provider import EpisodeRecord
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
import modules.memory.registry as reg

T0 = 1_800_000_000.0
RAIL = "cron:j1"


class _Container:
    def __init__(self, data_dir):
        self.config = types.SimpleNamespace(data_dir=str(data_dir))
        self._svc = {}

    def get_service(self, name):
        return self._svc.get(name)

    def register_service(self, name, svc):
        self._svc[name] = svc


class _Llm:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    async def ainvoke(self, msgs):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return types.SimpleNamespace(content=self.answer)


def _agent(llm):
    async def provision(task):
        assert task == "judge"
        return llm
    return types.SimpleNamespace(_provision_aux_llm_async=provision, _judge_llm=None, llm=None,
                                 agent_id="a1", orchestrator=None)


@pytest.fixture
def container(tmp_path):
    ot._BOUND_REPLIES.clear()
    yield _Container(tmp_path)
    ot._BOUND_REPLIES.clear()


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(p)
    yield p
    reg.reset_memory_registry()


def _episode(session_id, ts, rail=RAIL, **meta):
    return EpisodeRecord(ts=ts, started_ts=None, user_id="u1", session_id=session_id,
                         thread_key=rail, kind="cron", task="t", outcome="done", summary="s",
                         artifacts=[], spend_usd=0.0, steps=1, goal_id=None, meta=meta)


def _store(provider, *episodes):
    for e in episodes:
        asyncio.run(provider.record_episode(e, session_id=e.session_id, user_id="u1"))


def _meta(provider, session_id):
    con = sqlite3.connect(provider.db_path)
    try:
        return json.loads(con.execute("SELECT meta FROM episodes WHERE session_id=?",
                                      (session_id,)).fetchone()[0])
    finally:
        con.close()


def _sent(container, **kw):
    args = dict(via="telegram", session_id="s-run", source="cron", mid="900", rail_id=RAIL,
                now=T0)
    args.update(kw)
    ot.record_owner_out(container, "u1", "Posted the weekly note.", **args)


def _reply(container, text="no, stop posting that", **kw):
    args = dict(via="telegram", session_id="dm", mid="901", now=T0 + 60)
    args.update(kw)
    ot.record_owner_in(container, "u1", text, **args)


def _in_row(container):
    return [r for r in ot.thread_recent(container, "u1", limit=9) if r["direction"] == "in"][-1]


# --- bind -----------------------------------------------------------------------------

def test_quote_reply_binds_to_the_outgoing_row(container):
    _sent(container)
    ot.record_owner_out(container, "u1", "chat line after it", via="telegram", session_id="dm",
                        source="agent_send", mid="950", rail_id="", now=T0 + 30)
    _reply(container, reply_to_mid="900")
    assert _in_row(container)["rail_id"] == RAIL
    held = ot.take_bound_replies("dm")
    assert [(h["rail_id"], h["rail_text"]) for h in held] == [(RAIL, "Posted the weekly note.")]
    assert ot.take_bound_replies("dm") == []


def test_plain_reply_uses_the_existing_rule(container):
    _sent(container)
    _reply(container)
    assert _in_row(container)["rail_id"] == RAIL
    assert any(r.get("plain_reply") for r in ot.rail_slice(container, "u1", rail_id=RAIL))


@pytest.mark.parametrize("case", ["chat_in_between", "window_passed", "command", "card_tap",
                                  "reply_to_own_line", "no_rail"])
def test_unbound_reply_holds_nothing(container, case):
    if case != "no_rail":
        _sent(container)
    kw = {}
    if case == "chat_in_between":
        ot.record_owner_out(container, "u1", "chat", via="telegram", session_id="dm",
                            source="agent_send", mid="950", rail_id="", now=T0 + 30)
    elif case == "window_passed":
        kw["now"] = T0 + ot.PLAIN_REPLY_WINDOW_S + 1
    elif case in ("command", "card_tap"):
        kw["kind"] = case
    elif case == "reply_to_own_line":
        _reply(container, "first", mid="800", now=T0 + 10, session_id="other")
        kw["reply_to_mid"] = "800"
    _reply(container, **kw)
    assert _in_row(container)["rail_id"] == ""
    assert ot.take_bound_replies("dm") == []


def test_a_duplicate_line_is_held_once(container):
    _sent(container)
    _reply(container)
    _reply(container, now=T0 + 61)
    assert len(ot.take_bound_replies("dm")) == 1


def test_a_binding_fault_keeps_the_owner_line(container, monkeypatch):
    _sent(container)
    monkeypatch.setattr(ot.OwnerThreadStore, "answered_rail_line",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
    _reply(container)
    assert _in_row(container)["rail_id"] == ""
    assert ot.take_bound_replies("dm") == []


# --- classify and write ---------------------------------------------------------------

def _turn_end(container, llm, session="dm"):
    async def go():
        OV.owner_turn_ended(session, agent=_agent(llm))
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
    asyncio.run(go())


CORRECT = '{"verdict": "correct", "quote": "stop posting that"}'


def test_a_correction_reaches_the_latest_episode_and_the_signal(container, provider):
    _store(provider, _episode("older", 100), _episode("newest", 200, reason="kept"),
           _episode("other-rail", 300, rail="cron:j2"))
    _sent(container, session_id="newest")    # the run that sent it
    _reply(container)
    llm = _Llm(CORRECT)
    _turn_end(container, llm)
    assert llm.calls == 1
    meta = _meta(provider, "newest")
    assert meta["owner_verdict"] == "correct" and meta["signal_source"] == "owner"
    assert meta["owner_verdict_quote"] == "stop posting that" and meta["reason"] == "kept"
    assert "owner_verdict" not in _meta(provider, "older")
    assert "owner_verdict" not in _meta(provider, "other-rail")


def test_the_job_sheet_shows_the_verdict(container, provider):
    _store(provider, _episode("newest", 200))
    _sent(container, session_id="newest")    # the run that sent it
    _reply(container)
    _turn_end(container, _Llm(CORRECT))
    row = asyncio.run(provider.recall_episodes(user_id="u1", thread_key=RAIL, limit=1))[0]
    assert "owner_verdict=correct" in ED._track_record(row, row.meta)


def test_a_receipt_still_outranks_the_owner_word(container, provider):
    _store(provider, _episode("newest", 200, useful=True, signal_source="receipt"))
    _sent(container, session_id="newest")    # the run that sent it
    _reply(container)
    _turn_end(container, _Llm(CORRECT))
    meta = _meta(provider, "newest")
    assert meta["owner_verdict"] == "correct" and meta["signal_source"] == "receipt"


def test_a_neutral_reply_is_no_owner_signal_and_hides_no_verdict(container, provider):
    _store(provider, _episode("newest", 200))
    _sent(container, session_id="newest")    # the run that sent it
    _reply(container, "what time is it?")
    _turn_end(container, _Llm('{"verdict": "neutral", "quote": "what time is it?"}'))
    meta = _meta(provider, "newest")
    assert meta["owner_verdict"] == "neutral" and meta["signal_source"] != "owner"

    asyncio.run(OV.write_verdict("u1", RAIL, "correct", "wrong"))
    asyncio.run(OV.write_verdict("u1", RAIL, "neutral", "ok then"))
    assert _meta(provider, "newest")["owner_verdict"] == "correct"


def test_an_unbound_reply_makes_no_call_and_writes_nothing(container, provider):
    _store(provider, _episode("newest", 200))
    _reply(container)                                   # the owner received nothing before it
    llm = _Llm(CORRECT)
    _turn_end(container, llm)
    assert llm.calls == 0 and "owner_verdict" not in _meta(provider, "newest")


@pytest.mark.parametrize("answer", [RuntimeError("provider down"), "no json here",
                                    '{"verdict": "great"}', '{"quote": "x"}'])
def test_a_failed_classifier_writes_nothing_and_logs_one_line(container, provider, caplog, answer):
    _store(provider, _episode("newest", 200))
    _sent(container)
    _reply(container)
    llm = _Llm(answer)
    with caplog.at_level("WARNING", logger=OV.logger.name):
        _turn_end(container, llm)
    assert llm.calls == 1                               # no retry
    assert "owner_verdict" not in _meta(provider, "newest")
    assert len([r for r in caplog.records if r.name == OV.logger.name]) == 1


def test_the_turn_end_does_not_wait_for_the_classifier(container, provider):
    _store(provider, _episode("newest", 200))
    _sent(container)
    _reply(container)

    async def go():
        release = asyncio.Event()
        llm = _Llm(CORRECT)
        real = llm.ainvoke

        async def slow(msgs):
            await release.wait()
            return await real(msgs)
        llm.ainvoke = slow
        OV.owner_turn_ended("dm", agent=_agent(llm))    # returns with the call still open
        assert llm.calls == 0
        release.set()
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
        assert llm.calls == 1
    asyncio.run(go())


def test_a_session_still_running_keeps_its_replies(container):
    _sent(container)
    _reply(container)
    OV.owner_turn_ended("dm", agent=_agent(_Llm(CORRECT)), result="Session is already executing")
    assert len(ot.take_bound_replies("dm")) == 1


def test_lines_of_one_turn_on_one_rail_make_one_call(container, provider):
    _store(provider, _episode("newest", 200))
    _sent(container)
    _reply(container, "stop posting that", mid="901")
    _reply(container, "it reads badly", mid="902", now=T0 + 70)
    llm = _Llm(CORRECT)
    _turn_end(container, llm)
    assert llm.calls == 1


def test_a_quote_that_is_not_the_owner_words_gives_way():
    assert OV.parse_verdict('{"verdict": "confirm", "quote": "invented"}', "looks fine") == (
        "confirm", "looks fine")
    long = "x" * 500
    assert len(OV.parse_verdict('{"verdict": "confirm"}', long)[1]) == OV.QUOTE_MAX_CHARS


def test_a_new_record_of_the_run_makes_the_write_miss(provider):
    _store(provider, _episode("newest", 200))
    assert not provider.update_episode_meta(user_id="u1", session_id="newest", ts=199,
                                            patch={"owner_verdict": "correct"})
    assert provider.update_episode_meta(user_id="u1", session_id="newest", ts=200,
                                        patch={"owner_verdict": "correct"})
    assert not provider.update_episode_meta(user_id="u2", session_id="newest", ts=200,
                                            patch={"owner_verdict": "neutral"})
    assert _meta(provider, "newest")["owner_verdict"] == "correct"


def test_no_memory_store_writes_nothing():
    reg.reset_memory_registry()
    assert asyncio.run(OV.write_verdict("u1", RAIL, "correct", "q")) is False


def test_a_non_ascii_quote_is_held_to_its_json_size():
    import json as _json
    from agents.task.agent.core import owner_verdict as ov
    owner = "исправь это " * 40
    verdict, quote = ov.parse_verdict('{"verdict": "correct", "quote": ""}', owner)
    assert verdict == "correct" and quote
    assert len(_json.dumps(quote)) <= ov.QUOTE_MAX_JSON_CHARS


def test_no_episode_on_the_rail_makes_no_paid_call(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from agents.task.agent.core import owner_verdict as ov
    import modules.memory.registry as registry

    async def no_rows(**_kw):
        return []

    async def no_call(*_a, **_k):
        raise AssertionError("the judge ran with no episode to stamp")

    monkeypatch.setattr(registry, "memory_recall_episodes", no_rows)
    monkeypatch.setattr(ov, "_judge_llm", no_call)
    assert asyncio.run(ov.classify_reply(SimpleNamespace(), None, user_id="u", session_id="s",
                                         rail_id="job:1", rail_text="t", owner_text="o")) is False


def test_the_verdict_goes_on_the_answered_run_not_a_newer_one(container, provider):
    """The owner answers the 10:00 run after the 11:00 run ended: the 10:00 run is judged."""
    _store(provider, _episode("s-run", 100), _episode("s-later-run", 200))
    _sent(container)                                     # sent by session "s-run"
    _reply(container)
    assert ot.take_bound_replies("dm")[0]["rail_session_id"] == "s-run"
    _sent(container)
    _reply(container, mid="902", now=T0 + 61)
    _turn_end(container, _Llm(CORRECT))
    assert _meta(provider, "s-run")["owner_verdict"] == "correct"
    assert "owner_verdict" not in _meta(provider, "s-later-run")


def test_a_later_confirm_does_not_erase_a_correction(provider):
    _store(provider, _episode("newest", 200))
    reviews = []
    hook = lambda episode, meta: reviews.append(meta["owner_verdict"])  # noqa: E731
    assert asyncio.run(OV.write_verdict("u1", RAIL, "correct", "wrong", on_correct=hook))
    asyncio.run(OV.write_verdict("u1", RAIL, "confirm", "ok thanks", on_correct=hook))
    assert _meta(provider, "newest")["owner_verdict"] == "correct"
    asyncio.run(OV.write_verdict("u1", RAIL, "correct", "still wrong", on_correct=hook))
    assert reviews == ["correct"]                        # one review per corrected run


def test_a_reply_to_a_run_with_no_recent_episode_goes_on_no_other_run(container, provider):
    """The answered run is not among the recent episodes: the newest is another run, and
    the verdict must not be put on it."""
    _store(provider, _episode("s-later-run", 200))
    _sent(container)                                     # sent by session "s-run"
    _reply(container)
    _turn_end(container, _Llm(CORRECT))
    assert "owner_verdict" not in _meta(provider, "s-later-run")
