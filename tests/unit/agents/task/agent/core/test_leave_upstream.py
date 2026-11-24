"""A repeated step makes the run leave the upstream that served it, with no list of names."""
import asyncio
import logging
from collections import deque
from types import SimpleNamespace

import pytest

from agents.task.agent.core.call_quality import step_call_scope
from agents.task.agent.core.loop_detection import LoopDetectionMixin
from modules.llm.call_scope import avoided_upstreams, call_scope, set_avoided_upstreams
from modules.llm.openrouter_routing import (
    apply_provider_routing, create_completion, create_with_tool_choice_fallback,
    provider_routing_extra_body)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for name in ("SORT", "ORDER", "IGNORE"):
        monkeypatch.delenv(f"OPENROUTER_PROVIDER_{name}", raising=False)


class _Action:
    def __init__(self, name, **params):
        self._d = {name: params}

    def model_dump(self, **_):
        return self._d


class _Agent(LoopDetectionMixin):
    def __init__(self):
        self.logger = logging.getLogger("t")
        self._previous_actions = deque(maxlen=25)
        self._action_repetition_counter = 0
        self._max_allowed_repetitions = 3
        self._reset_loop_guard()

    def _trigger_loop_intervention(self, reason, **_):
        pass

    def step(self, upstream, name="read", text="same", error=None, **params):
        self.last_call_facts = SimpleNamespace(upstream=upstream)
        self._last_result = [SimpleNamespace(error=error, extracted_content=text)]
        self._record_action_for_loop_detection(
            SimpleNamespace(action=[_Action(name, **params)], current_state=None))
        return self._left_upstreams


def test_the_same_call_with_the_same_result_leaves_the_upstream():
    a = _Agent()
    assert a.step("UpA") == []
    assert a.step("UpA") == ["UpA"]


def test_the_same_call_that_fails_again_leaves_the_upstream():
    a = _Agent()
    a.step("UpA", error="Missing required parameter: room", limit=5)
    assert a.step("UpA", error="Missing required parameter: room (2)", limit=5) == ["UpA"]


def test_the_next_weak_upstream_is_left_too():
    a = _Agent()
    for up in ("UpA", "UpA", "UpB"):
        a.step(up)
    assert a._left_upstreams == ["UpA", "UpB"]


def test_a_poll_whose_result_changes_stays():
    a = _Agent()
    a.step("UpA", text="10%")
    assert a.step("UpA", text="20%") == []


def test_a_wait_stays():
    a = _Agent()
    a.step("UpA", name="process_wait")
    assert a.step("UpA", name="process_wait") == []


def test_other_arguments_stay():
    a = _Agent()
    a.step("UpA", page=1)
    assert a.step("UpA", page=2) == []


def test_a_call_the_guard_refused_leaves_the_upstream():
    a = _Agent()
    a._loop_blocked_step = True
    assert a.step("UpA") == ["UpA"]


def test_a_two_call_cycle_leaves_the_upstream():
    a = _Agent()
    for name in ("x", "y", "x", "y"):
        a.step("UpA", name=name)
    assert a._left_upstreams == ["UpA"]


def test_a_provider_with_one_server_changes_nothing():
    a = _Agent()
    a.step(None)
    assert a.step(None) == []


def test_a_new_run_forgets():
    a = _Agent()
    a.step("UpA")
    a.step("UpA")
    a._reset_loop_guard()
    assert a._left_upstreams == []


def test_the_step_scope_sends_it_and_ends_it():
    agent = SimpleNamespace(message_manager=SimpleNamespace(), session_id="s", _left_upstreams=["UpA"])
    with step_call_scope(agent):
        assert provider_routing_extra_body("m") == {"provider": {"ignore": ["UpA"]}}
    assert avoided_upstreams() == ()
    assert provider_routing_extra_body("m") is None


def test_the_operator_list_is_kept(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER_IGNORE", "UpA,UpZ")
    with call_scope():
        set_avoided_upstreams(["UpA", "UpB"])
        assert provider_routing_extra_body("m") == {"provider": {"ignore": ["UpA", "UpZ", "UpB"]}}


class _Sdk:
    def __init__(self, *outcomes):
        self.sent, self._outcomes = [], list(outcomes)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **params):
        self.sent.append({**params, "extra_body": dict(params.get("extra_body") or {})})
        out = self._outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


_NO_ROUTE = Exception("Error code: 404 - All providers have been ignored.")


def test_the_only_route_is_used_again(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER_IGNORE", "UpZ")
    sdk = _Sdk(_NO_ROUTE, "ok")
    with call_scope():
        set_avoided_upstreams(["UpA"])
        params = apply_provider_routing({"model": "m", "tool_choice": "required"})
        client = SimpleNamespace(model_type="m", logger=None)
        assert asyncio.run(create_with_tool_choice_fallback(client, sdk, params)) == "ok"
    assert [s["extra_body"]["provider"]["ignore"] for s in sdk.sent] == [["UpZ", "UpA"], ["UpZ"]]
    assert sdk.sent[-1]["tool_choice"] == "required"


def test_an_empty_block_is_not_sent_again():
    sdk = _Sdk(_NO_ROUTE, "ok")
    with call_scope():
        set_avoided_upstreams(["UpA"])
        params = apply_provider_routing({"model": "m"})
        assert asyncio.run(create_completion(sdk, params)) == "ok"
    assert sdk.sent[-1]["extra_body"] == {}


@pytest.mark.parametrize("error", [_NO_ROUTE, Exception("rate limit")])
def test_with_nothing_left_the_error_stands(error):
    sdk = _Sdk(error)
    with pytest.raises(Exception, match=str(error)[:8]):
        asyncio.run(create_completion(sdk, {"model": "m"}))
    assert len(sdk.sent) == 1
