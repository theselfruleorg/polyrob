"""UP-10 2.1: _validate_output routes through the optional 'judge' aux model.

Default (no _judge_llm) is byte-identical to the legacy self.llm path; when a judge
model is provisioned it is used for the validator; provisioning failure is fail-open.

P0-6 (2026-07-07): the judge is fail-OPEN end-to-end on the live manual-parse path —
an unparseable/erroring/hanging judge reply must return True (pass), never raise out
of the run loop and kill the turn after done(). A well-formed
``{"is_valid": false}`` must still return False (that's the feature).
"""
import asyncio
import types

from agents.task.agent.core.output_validation import OutputValidationMixin


class _FakeLLM:
    def __init__(self, name, parsed_valid=True):
        self.name = name
        self.parsed_valid = parsed_valid
        self.used = False

    def with_structured_output(self, model, include_raw=True):
        self.used = True
        outer = self

        class _Validator:
            async def ainvoke(self, msg):
                return {"parsed": model(is_valid=outer.parsed_valid, reason=f"by {outer.name}")}

        return _Validator()


class _FakeSession:
    pass


class _FakeBrowserCtx:
    def __init__(self):
        self.session = _FakeSession()

    async def get_state(self):
        return types.SimpleNamespace()


def _make_agent(judge_llm, main_llm):
    """Minimal object exposing just what _validate_output touches."""
    import logging

    agent = OutputValidationMixin.__new__(OutputValidationMixin)
    agent.llm = main_llm
    if judge_llm is not None:
        agent._judge_llm = judge_llm
    agent.task = "do a thing"
    agent._last_result = []
    agent.include_attributes = []
    agent.max_error_length = 400
    agent.use_vision = False
    agent.logger = logging.getLogger("test_judge")

    async def _get_browser_context():
        return _FakeBrowserCtx()

    agent.get_browser_context = _get_browser_context

    # AgentMessagePrompt.get_user_message needs a usable content; patch the prompt build
    # to avoid pulling real browser state through. We replace the message list build by
    # stubbing get_user_message via monkeypatching the class at call time.
    return agent


def test_judge_model_used_when_provisioned(monkeypatch):
    judge = _FakeLLM("judge")
    main = _FakeLLM("main")
    agent = _make_agent(judge_llm=judge, main_llm=main)

    # Stub AgentMessagePrompt so we don't need real browser state.
    import agents.task.agent.core.output_validation as ov

    class _StubPrompt:
        def __init__(self, **kw):
            pass

        def get_user_message(self, use_vision):
            from modules.llm.messages import HumanMessage
            return HumanMessage(content="state")

    monkeypatch.setattr(ov, "AgentMessagePrompt", _StubPrompt)

    ok = asyncio.run(agent._validate_output())
    assert ok is True
    assert judge.used is True, "judge model should build the validator"
    assert main.used is False, "main model must NOT be used when a judge is provisioned"


def test_main_model_used_when_no_judge(monkeypatch):
    main = _FakeLLM("main")
    agent = _make_agent(judge_llm=None, main_llm=main)

    import agents.task.agent.core.output_validation as ov

    class _StubPrompt:
        def __init__(self, **kw):
            pass

        def get_user_message(self, use_vision):
            from modules.llm.messages import HumanMessage
            return HumanMessage(content="state")

    monkeypatch.setattr(ov, "AgentMessagePrompt", _StubPrompt)

    ok = asyncio.run(agent._validate_output())
    assert ok is True
    assert main.used is True, "default path must use the main model"


# ---------------------------------------------------------------------------
# P0-6: live manual-parse path — fail-OPEN on unparseable/error/timeout, but a
# well-formed {"is_valid": false} still fails the validation (regression).
# ---------------------------------------------------------------------------


class _ManualLLM:
    """Judge LLM WITHOUT with_structured_output → exercises the manual-parse path."""

    def __init__(self, content=None, exc=None, hang=0.0):
        self.content = content
        self.exc = exc
        self.hang = hang
        self.calls = 0

    async def ainvoke(self, msg):
        self.calls += 1
        if self.hang:
            await asyncio.sleep(self.hang)
        if self.exc is not None:
            raise self.exc
        return types.SimpleNamespace(content=self.content)


def _stub_prompt(monkeypatch):
    import agents.task.agent.core.output_validation as ov

    class _StubPrompt:
        def __init__(self, **kw):
            pass

        def get_user_message(self, use_vision):
            from modules.llm.messages import HumanMessage
            return HumanMessage(content="state")

    monkeypatch.setattr(ov, "AgentMessagePrompt", _StubPrompt)
    return ov


def _record_metering(monkeypatch):
    """Replace meter_aux_llm with a recorder; returns the call list."""
    import agents.task.agent.core.aux_metering as am

    calls = []

    async def _fake_meter(**kw):
        calls.append(kw)

    monkeypatch.setattr(am, "meter_aux_llm", _fake_meter)
    return calls


def test_prose_reply_fails_open(monkeypatch):
    """Judge replies with prose (no JSON) → pass, no exception (was: ValueError
    from extract_json_from_model_output escaping the run loop after done())."""
    _stub_prompt(monkeypatch)
    judge = _ManualLLM(content="The agent did a great job overall, well done.")
    agent = _make_agent(judge_llm=judge, main_llm=_FakeLLM("main"))

    ok = asyncio.run(agent._validate_output())
    assert ok is True
    assert judge.calls == 1


def test_judge_exception_fails_open(monkeypatch):
    """Judge LLM raises (e.g. network error) → pass, no exception."""
    _stub_prompt(monkeypatch)
    metered = _record_metering(monkeypatch)
    judge = _ManualLLM(exc=RuntimeError("simulated network error"))
    agent = _make_agent(judge_llm=judge, main_llm=_FakeLLM("main"))

    ok = asyncio.run(agent._validate_output())
    assert ok is True
    assert metered == [], "must not meter a call that never returned"


def test_judge_timeout_fails_open(monkeypatch):
    """Judge ainvoke hangs past the (injected, short) timeout → pass."""
    ov = _stub_prompt(monkeypatch)
    monkeypatch.setattr(ov, "VALIDATION_JUDGE_TIMEOUT_SEC", 0.05)
    judge = _ManualLLM(content='{"is_valid": true, "reason": "late"}', hang=2.0)
    agent = _make_agent(judge_llm=judge, main_llm=_FakeLLM("main"))

    ok = asyncio.run(agent._validate_output())
    assert ok is True


def test_invalid_verdict_still_fails(monkeypatch):
    """Regression: a well-formed {"is_valid": false} must still return False —
    fail-open covers judge FAILURES, never a judge VERDICT."""
    _stub_prompt(monkeypatch)
    metered = _record_metering(monkeypatch)
    judge = _ManualLLM(content='{"is_valid": false, "reason": "output missing the file"}')
    agent = _make_agent(judge_llm=judge, main_llm=_FakeLLM("main"))

    ok = asyncio.run(agent._validate_output())
    assert ok is False
    assert agent._last_result, "invalid verdict must feed a corrective result back"
    assert "output missing the file" in (agent._last_result[0].extracted_content or "")
    assert len(metered) == 1, "successful judge call must be metered"


def test_malformed_json_keyword_fallback_still_fails(monkeypatch):
    """Malformed JSON that still clearly states is_valid: false → False via the
    tolerant keyword fallback (mirrors goals/completion_judge's regex ladder)."""
    _stub_prompt(monkeypatch)
    judge = _ManualLLM(content='Verdict — "is_valid": false, because the answer is wrong')
    agent = _make_agent(judge_llm=judge, main_llm=_FakeLLM("main"))

    ok = asyncio.run(agent._validate_output())
    assert ok is False


def test_structured_parsed_none_fails_open(monkeypatch):
    """LOW-1: structured path returning {'parsed': None, 'raw': ...} → pass,
    not a crash on parsed.is_valid."""
    _stub_prompt(monkeypatch)

    class _NoneParsedLLM:
        def with_structured_output(self, model, include_raw=True):
            class _Validator:
                async def ainvoke(self, msg):
                    return {"parsed": None, "raw": object()}

            return _Validator()

    agent = _make_agent(judge_llm=_NoneParsedLLM(), main_llm=_FakeLLM("main"))

    ok = asyncio.run(agent._validate_output())
    assert ok is True


# ---------------------------------------------------------------------------
# P2-8: the cached judge client is closed at cleanup, and the reviewer/goal-judge
# reuse it instead of leaking a fresh httpx pool per fire.
# ---------------------------------------------------------------------------


def test_p2_8_cleanup_closes_judge_client():
    import inspect
    from agents.task.session import cleanup
    src = inspect.getsource(cleanup)
    assert "'_judge_llm'" in src, "M2 cleanup must close the cached judge client"


def test_p2_8_background_review_reuses_cached_judge():
    import inspect
    from agents.task.agent.core import background_review
    src = inspect.getsource(background_review)
    # reads the cache before provisioning, and writes back a freshly provisioned one
    assert 'getattr(self, "_judge_llm", None)' in src
    assert "self._judge_llm = aux" in src


def test_p2_8_goal_judge_writes_back_cache():
    import inspect
    from agents.task.goals import completion_judge
    src = inspect.getsource(completion_judge)
    assert "agent._judge_llm = llm" in src
