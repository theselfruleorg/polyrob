"""§3.2/§4.3 — completion judge: verdict parsing and the shared judge call.

The judge is evidence-in/judgment-out: code supplies the RunOutcome evidence pack,
a cheap aux model judges the completion CLAIM, and everything fails OPEN (never
block on uncertainty, never crash the dispatcher).
"""
import asyncio
from types import SimpleNamespace

from modules.goals.board import Goal
from agents.task.goals.completion_judge import (
    judge_run_outcome,
    parse_judge_response,
    parse_verdict,
)


# --- fakes -------------------------------------------------------------------

class _Action:
    def __init__(self, name):
        self._name = name

    def model_dump(self, exclude_none=True):
        return {self._name: {"arg": 1}, "interacted_element": None}


class _Result:
    def __init__(self, error=None, content=None):
        self.error = error
        self.extracted_content = content


class _Step:
    def __init__(self, actions, results):
        self.model_output = type("MO", (), {"action": actions})()
        self.result = results


class _FakeAgent:
    def __init__(self, steps, is_sub=False):
        self.history = type("H", (), {"history": steps})()
        self._is_sub_agent = is_sub


class _FakeOrch:
    def __init__(self, agents):
        self.agents = agents




# --- verdict parsing -----------------------------------------------------------

def test_parse_verdict_variants():
    assert parse_verdict({"verdict": "met", "reason": "posted"}) == ("met", "posted")
    assert parse_verdict({"verdict": "UNMET", "reason": "draft only"}) == ("unmet", "draft only")
    assert parse_verdict({"verdict": "unclear"})[0] == "unclear"
    # anything unrecognised fails open to unclear
    assert parse_verdict({"verdict": "banana"})[0] == "unclear"
    assert parse_verdict({})[0] == "unclear"


# --- judge response parsing (prod 2026-07-05: deepseek wrapped/omitted JSON and the
# --- agent-schema extractor RAISED -> fail-open masked every verdict) --------------

def test_parse_judge_response_pure_json():
    assert parse_judge_response('{"verdict": "met", "reason": "posted"}') == ("met", "posted")


def test_parse_judge_response_fenced_json():
    text = "```json\n{\"verdict\": \"unmet\", \"reason\": \"only a draft\"}\n```"
    assert parse_judge_response(text) == ("unmet", "only a draft")


def test_parse_judge_response_prose_wrapped_json():
    text = 'Looking at the evidence... {"verdict": "unmet", "reason": "twitter_post errored"} Hope this helps!'
    assert parse_judge_response(text) == ("unmet", "twitter_post errored")


def test_parse_judge_response_malformed_json_regex_fallback():
    text = '{"verdict": "unmet", "reason": "quote failed", extra garbage'
    verdict, reason = parse_judge_response(text)
    assert verdict == "unmet"
    assert "quote failed" in reason


def test_parse_judge_response_garbage_is_unclear_never_raises():
    for garbage in ("The task looks fine to me.", "", None, "verdict met-ish maybe"):
        verdict, _ = parse_judge_response(garbage)
        assert verdict == "unclear"


# --- judge_run_outcome (fail-open + happy path) ----------------------------------

def _goal(acceptance="a live tweet URL"):
    return Goal(id="g1", user_id="u1", title="Post announcement",
                payload={"acceptance": acceptance})


def _run(claim, ledger=()):
    return SimpleNamespace(done_text=claim, artifacts=[],
                           evidence=SimpleNamespace(ledger=list(ledger), captured_refs=[],
                                                    checks=[]))


def test_judge_fails_open_when_orchestrator_lookup_raises():
    class _TA:
        def get_orchestrator(self, sid):
            raise RuntimeError("gone")

    verdict, reason = asyncio.run(
        judge_run_outcome(_TA(), "s1", _goal(), _run("done")))
    assert verdict == "unclear"


def test_judge_fails_open_when_no_llm_available():
    class _TA:
        def get_orchestrator(self, sid):
            return _FakeOrch(agents={})

    verdict, _ = asyncio.run(judge_run_outcome(_TA(), "s1", _goal(), _run("done")))
    assert verdict == "unclear"


def test_judge_retries_once_when_model_narrates_instead_of_json():
    """Prod 2026-07-05 17:40Z: deepseek replies 'Let me examine the evidence...'
    (agentic narration, no JSON). One corrective retry must recover the verdict;
    the retry prompt reminds the judge it has no tools and must output JSON only."""
    replies = [
        "I need to analyze the acceptance criteria against the evidence. Let me read the file.",
        '{"verdict": "unmet", "reason": "no live URL; engagement never reached the thread"}',
    ]

    class _LLM:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, msgs):
            self.calls.append(msgs)
            return type("R", (), {"content": replies[len(self.calls) - 1]})()

    llm = _LLM()

    class _JudgeAgent(_FakeAgent):
        async def _provision_aux_llm_async(self, task):
            return llm

    orch = _FakeOrch(agents={"main": _JudgeAgent([_Step([_Action("twitter_quote")],
                                                        [_Result(error="403 Forbidden")])])})

    class _TA:
        def get_orchestrator(self, sid):
            return orch

    verdict, reason = asyncio.run(judge_run_outcome(_TA(), "s1", _goal(), _run("done")))
    assert verdict == "unmet"
    assert len(llm.calls) == 2, "exactly one corrective retry"


def test_judge_double_garbage_is_unclear():
    class _LLM:
        def __init__(self):
            self.n = 0

        async def ainvoke(self, msgs):
            self.n += 1
            return type("R", (), {"content": "Let me think about this some more."})()

    llm = _LLM()

    class _JudgeAgent(_FakeAgent):
        async def _provision_aux_llm_async(self, task):
            return llm

    orch = _FakeOrch(agents={"main": _JudgeAgent([])})

    class _TA:
        def get_orchestrator(self, sid):
            return orch

    verdict, _ = asyncio.run(judge_run_outcome(_TA(), "s1", _goal(), _run("done")))
    assert verdict == "unclear"
    assert llm.n == 2, "retry once, then give up fail-open"


def test_judge_happy_path_unmet():
    class _Raw:
        content = '{"verdict": "unmet", "reason": "twitter_post errored; only a draft file was written"}'

    class _LLM:
        async def ainvoke(self, msgs):
            self.msgs = msgs
            return _Raw()

    llm = _LLM()

    class _JudgeAgent(_FakeAgent):
        async def _provision_aux_llm_async(self, task):
            assert task == "judge"
            return llm

    steps = [_Step([_Action("twitter_post")], [_Result(error="Twitter write surface disabled")])]
    orch = _FakeOrch(agents={"main": _JudgeAgent(steps)})

    class _TA:
        def get_orchestrator(self, sid):
            return orch

    verdict, reason = asyncio.run(
        judge_run_outcome(_TA(), "s1", _goal(), _run(
            "OUTCOME: posted (draft saved)",
            ["twitter_post -> ERROR: Twitter write surface disabled"])))
    assert verdict == "unmet"
    assert "draft" in reason
    # the judge saw the acceptance AND the ledger as evidence
    joined = " ".join(str(getattr(m, "content", m)) for m in llm.msgs)
    assert "a live tweet URL" in joined and "twitter_post" in joined
