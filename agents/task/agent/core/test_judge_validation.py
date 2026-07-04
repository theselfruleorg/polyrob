"""UP-10 2.1: _validate_output routes through the optional 'judge' aux model.

Default (no _judge_llm) is byte-identical to the legacy self.llm path; when a judge
model is provisioned it is used for the validator; provisioning failure is fail-open.
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
