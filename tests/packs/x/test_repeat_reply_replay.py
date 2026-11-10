"""108 W2: the replay on both X rails, through the hooks the Controller installs.

The second reply to one post is refused, on either rail, on a standing job's main run. The
owner typing passes and still writes a row. A failed act does not block a retry. A swap never
reaches the check. A read fault lets the post through and logs.
"""
import asyncio
import logging
import time
import types

import pytest

pytest.importorskip("polyrob_x")

import core.event_log as el
from polyrob_x.twitter_tool import TwitterQuoteAction, TwitterReplyAction
from polyrob_x.x_browser.tool import XReplyAction

POST = "1234567890"
URL = f"https://x.com/someone/status/{POST}"
MODELS = {"twitter_reply": ("twitter", TwitterReplyAction),
          "twitter_quote": ("twitter", TwitterQuoteAction),
          "x_browser_x_reply": ("x_browser", XReplyAction),
          "defi_trade_swap": ("defi_trade", None),
          "message": (None, None)}


class _Controller:
    _is_sub_agent = False
    session_id = "s1"
    user_id = "u1"

    def get_action_details(self, name):
        tool, model = MODELS[name]
        return types.SimpleNamespace(tool=tool, function=None, param_model=model)


class _Ctx:
    user_id = "u1"
    session_id = "s1"
    role = "orchestrator"
    is_sub_agent = False

    def __init__(self):
        self.metadata = {}


@pytest.fixture
def tlog(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    el._INSTANCES.clear()
    yield lambda: el.get_event_log().query(kind="external_write")
    el._INSTANCES.clear()


@pytest.fixture
def job_run(monkeypatch):
    """A standing job's main run: is_owner_driven says True, the owner is not typing."""
    monkeypatch.setattr("core.autonomy_marker.is_autonomous", lambda sid: True)
    monkeypatch.setattr("core.autonomy_marker.job_task_for_session", lambda sid: "a job")
    monkeypatch.setattr("core.security.owner_turn.is_owner_tenant", lambda ctx: True)


@pytest.fixture
def owner_typing(monkeypatch):
    monkeypatch.setattr("core.autonomy_marker.is_autonomous", lambda sid: False)
    monkeypatch.setattr("core.security.owner_turn.is_owner_tenant", lambda ctx: True)


def _pre(action, params, ctx):
    from tools.controller.effect_hooks import make_repeat_act_hook
    return asyncio.run(make_repeat_act_hook(_Controller())(action, params, ctx))


def _post(action, params, ctx, *, error=None, text="🐦 Replied with tweet 7654321."):
    from tools.controller.effect_hooks import make_effect_record_hook
    result = types.SimpleNamespace(error=error, extracted_content=None if error else text)
    asyncio.run(make_effect_record_hook(_Controller())(action, params, result, ctx))


API = ("twitter_reply", {"tweet_id": POST, "text": "first"})
BROWSER = ("x_browser_x_reply", {"in_reply_to": URL, "text": "first"})
QUOTE = ("twitter_quote", {"tweet_id": POST, "text": "first"})


def test_the_job_run_really_is_owner_driven(tlog, job_run):
    from core.security.owner_turn import is_owner_driven, is_owner_typing
    assert is_owner_driven(_Ctx()) and not is_owner_typing(_Ctx(), _Controller())


@pytest.mark.parametrize("first, second", [
    (API, API), (BROWSER, BROWSER), (API, BROWSER), (BROWSER, API), (QUOTE, BROWSER),
], ids=["api-api", "browser-browser", "api-browser", "browser-api", "quote-browser"])
def test_a_second_reply_is_refused_with_the_fact(tlog, job_run, first, second):
    ctx = _Ctx()
    assert _pre(*first, ctx) is None
    _post(*first, ctx)
    refusal = _pre(*second, ctx)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert refusal.startswith(f"already replied to this post on {today} (7654321); skip it")
    assert "approv" not in refusal.lower() and "owner" not in refusal.lower()


def test_a_refused_reply_writes_a_denied_row_and_no_ask(tlog, job_run):
    ctx = _Ctx()
    _post(*API, ctx)
    assert _pre(*BROWSER, ctx)
    rows = tlog()
    assert [r["attrs"]["outcome"] for r in rows] == ["denied", "ok"]
    assert {r["attrs"]["subject"] for r in rows} == {POST}


def test_the_window_ends_after_thirty_days(tlog, job_run, monkeypatch):
    ctx = _Ctx()
    _post(*API, ctx)
    later = time.time() + 31 * 86400
    monkeypatch.setattr("time.time", lambda: later)
    assert _pre(*API, ctx) is None


def test_another_post_passes(tlog, job_run):
    ctx = _Ctx()
    _post(*API, ctx)
    assert _pre("twitter_reply", {"tweet_id": "999999", "text": "other"}, ctx) is None


def test_the_owner_typing_passes_and_still_writes_a_row(tlog, owner_typing):
    ctx = _Ctx()
    _post(*API, ctx)
    assert _pre(*API, ctx) is None
    assert _pre(*BROWSER, ctx) is None
    _post(*BROWSER, ctx, text=f"replied on X under status {POST}: https://x.com/me/status/7654322")
    rows = tlog()
    assert len(rows) == 2 and [r["attrs"]["ref"] for r in rows] == ["7654322", "7654321"]


def test_a_tainted_owner_turn_does_not_pass(tlog, owner_typing, monkeypatch):
    ctx = _Ctx()
    _post(*API, ctx)
    monkeypatch.setattr("core.security.taints.turn_tainted", lambda c: True)
    assert _pre(*API, ctx)


def test_a_failed_act_does_not_block_a_retry(tlog, job_run):
    ctx = _Ctx()
    _post(*API, ctx, error="429")
    assert _pre(*BROWSER, ctx) is None


def test_a_refusal_does_not_count_as_an_act(tlog, job_run):
    from core.effects import record_external_write
    record_external_write(effect="social", tool="twitter", action="twitter_reply",
                          outcome="denied", user_id="u1", session_id="s1", subject=POST)
    assert _pre(*API, _Ctx()) is None


def test_another_tenants_post_does_not_count(tlog, job_run):
    other = _Ctx()
    other.user_id = "u2"
    _post(*API, other)
    assert _pre(*API, _Ctx()) is None


def test_a_swap_is_untouched(tlog, job_run, monkeypatch):
    """The buyback buys the same amount each time: the same call, and it never reads the log."""
    reads = []
    monkeypatch.setattr("core.effects.last_good_act_on", lambda *a, **k: reads.append(a) or {})
    swap = {"token_in": "A", "token_out": "B", "amount": "1"}
    ctx = _Ctx()
    for _ in range(3):
        assert _pre("defi_trade_swap", swap, ctx) is None
    assert _pre("message", {"text": "a note"}, ctx) is None
    assert reads == []
    assert _pre(*API, ctx) is None
    assert len(reads) == 1  # the spy does see a verb with a subject


def test_no_money_verb_can_enter_through_the_pack():
    from core.verb_policy import VERB_POLICY
    subjects = {r.name for r in VERB_POLICY.values() if r.subject}
    assert subjects == {"twitter_reply", "twitter_quote", "x_browser_x_reply"}


def test_a_read_fault_fails_open_and_logs(tlog, job_run, monkeypatch, caplog):
    ctx = _Ctx()
    _post(*API, ctx)

    def _boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr("core.effects.last_good_act_on", _boom)
    with caplog.at_level(logging.WARNING, logger="tools.controller.effect_hooks"):
        assert _pre(*API, ctx) is None
    assert "repeat check failed" in caplog.text


def test_an_unreadable_store_fails_open_and_logs(tlog, job_run, monkeypatch, caplog):
    ctx = _Ctx()
    _post(*API, ctx)
    monkeypatch.setattr("core.event_log.execute_retry",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    with caplog.at_level(logging.WARNING, logger="tools.controller.effect_hooks"):
        assert _pre(*API, ctx) is None
    assert "unreadable" in caplog.text


def test_no_log_file_yet_passes(tlog, job_run):
    assert _pre(*API, _Ctx()) is None


# -- the real Controller: the chain it installs, multi_act, the refusal the model reads -------

class _Named:
    """An action object as multi_act reads it."""
    _tool_call_id = "call_1"

    def __init__(self, name, params):
        self._dump = {name: params}

    def model_dump(self, **kw):
        return dict(self._dump)

    def get_index(self):
        return None


def _real_controller(tmp_path, sent):
    import agents.task.agent.service  # noqa: F401
    from agents.task.loop_types.controller_types import ActionResult
    from tools.controller.service import Controller
    orch = types.SimpleNamespace(session_id="s1", user_id="u1", workspace_dir=str(tmp_path),
                                 _correspondent_tainted=False)
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    c = Controller(container=container, orchestrator=orch)

    @c.registry.action("reply", param_model=TwitterReplyAction, tool="twitter")
    async def twitter_reply(params: TwitterReplyAction, execution_context=None):
        sent.append(("api", params.tweet_id))
        return ActionResult(extracted_content="🐦 Replied with tweet 7654321.")

    @c.registry.action("reply", param_model=XReplyAction, tool="x_browser")
    async def x_browser_x_reply(params: XReplyAction, execution_context=None):
        sent.append(("browser", params.status_id))
        return ActionResult(extracted_content=f"replied on X under status {params.status_id}: "
                                              "https://x.com/me/status/7654322")

    @c.registry.action("swap", tool="defi_trade")
    async def defi_trade_swap(amount: str, execution_context=None):
        sent.append(("swap", amount))
        return ActionResult(extracted_content="swapped")

    return c


def _ctx_for_controller():
    ctx = _Ctx()
    ctx.browser_context = None
    ctx.sensitive_data = {}
    ctx.available_file_paths = []
    return ctx


async def _run(c, name, params, ctx):
    return (await c.multi_act([_Named(name, params)], execution_context=ctx))[0]


def test_replay_through_the_real_controller(tlog, job_run, tmp_path, monkeypatch):
    import core.effects
    reads = []
    real_read = core.effects.last_good_act_on

    def _spy(*a, **k):
        reads.append(a)
        return real_read(*a, **k)
    monkeypatch.setattr(core.effects, "last_good_act_on", _spy)
    sent = []
    c = _real_controller(tmp_path, sent)
    ctx = _ctx_for_controller()

    async def _go():
        first = await _run(c, "twitter_reply", {"tweet_id": POST, "text": "first"}, ctx)
        assert not first.error
        second = await _run(c, "x_browser_x_reply", {"in_reply_to": URL, "text": "again"}, ctx)
        assert "blocked: already replied to this post on" in second.error
        assert "(7654321); skip it" in second.error
        third = await _run(c, "twitter_reply", {"tweet_id": POST, "text": "again"}, ctx)
        assert "already replied" in third.error
        # The wallet gate answers this stub swap; the repeat check never reads for it.
        reads.clear()
        for _ in range(2):
            swap = await _run(c, "defi_trade_swap", {"amount": "1"}, ctx)
            assert "already replied" not in str(swap.error)
        assert reads == []
    asyncio.run(_go())
    assert sent == [("api", POST)]


def test_replay_owner_typing_through_the_real_controller(tlog, owner_typing, tmp_path):
    sent = []
    c = _real_controller(tmp_path, sent)
    ctx = _ctx_for_controller()

    async def _go():
        await _run(c, "x_browser_x_reply", {"in_reply_to": URL, "text": "first"}, ctx)
        again = await _run(c, "twitter_reply", {"tweet_id": POST, "text": "again"}, ctx)
        assert not again.error
    asyncio.run(_go())
    assert sent == [("browser", POST), ("api", POST)]
    rows = [r for r in tlog() if r["attrs"]["outcome"] == "ok" and r["effect"] == "social"]
    assert [r["attrs"]["subject"] for r in rows] == [POST, POST]


# -- a late owner approval sends through no Controller: its row still counts -----------------

def _late_send(monkeypatch, sender):
    from tools.controller import approval_queue as aq
    row = types.SimpleNamespace(
        user_id="u1", payload={"tool_name": "twitter_reply", "decision": "approved",
                               "outbound_write": True,
                               "exact_params": {"tweet_id": POST, "text": "first"}})

    class _Board:
        def __init__(self, *a):
            pass

        def get(self, ask_id):
            return row

        def consume_ask_grant(self, ask_id):
            return True

    async def _notify(*a, **k):
        return None
    monkeypatch.setattr("agents.task.goals.board.GoalBoard", _Board)
    monkeypatch.setattr(aq, "_outbound_write_tools", lambda: {"twitter_reply"})
    monkeypatch.setitem(aq._LATE_SENDERS, "twitter_reply", sender)
    monkeypatch.setattr(aq, "_push_owner_notification", _notify)
    return asyncio.run(aq.run_approved_outbound({"ask_id": "a1", "goals_db": "x"}, "u1"))


def test_a_late_approved_reply_counts_as_the_answer(tlog, job_run, monkeypatch):
    async def _sent(tool, params, agent):
        return True, "🐦 Replied with tweet 7654321."
    assert _late_send(monkeypatch, _sent)
    row = tlog()[0]
    assert row["user_id"] == "u1" and row["effect"] == "social"
    assert (row["attrs"]["subject"], row["attrs"]["ref"]) == (POST, "7654321")
    assert row["attrs"]["autonomous"] is False
    assert "already replied" in _pre(*BROWSER, _Ctx())


def test_a_late_send_that_failed_does_not_block_a_retry(tlog, job_run, monkeypatch):
    async def _failed(tool, params, agent):
        return False, "429"
    assert _late_send(monkeypatch, _failed)
    assert tlog()[0]["attrs"]["outcome"] == "error"
    assert _pre(*API, _Ctx()) is None
