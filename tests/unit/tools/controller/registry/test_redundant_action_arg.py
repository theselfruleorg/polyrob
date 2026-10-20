"""A redundant ``action`` arg that only restates the tool's own name is dropped.

Live, 2026-10-15 15:00Z (job 252e39429736): the X growth run called
``twitter_whoami(action='whoami')`` 4x in 5 s; every call failed with
``action  Extra inputs are not permitted`` and the run gave up reading its
follower count. A tool that DECLARES an ``action`` field keeps it untouched.
"""
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from tools.controller.registry.service import Registry


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ModerateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["mute", "unmute", "moderate"]


async def _noop(params=None, execution_context=None):
    from agents.task.agent.views import ActionResult
    return ActionResult(extracted_content="ok")


def _registry():
    reg = Registry()
    reg.wrap_function("twitter_whoami", _noop, "who am I", tool="twitter", param_model=_NoParams)
    reg.wrap_function("room_moderate", _noop, "moderate", tool="room", param_model=_ModerateParams)
    return reg


@pytest.mark.parametrize("value", ["whoami", "twitter_whoami", "WhoAmI"])
def test_action_naming_the_tool_itself_is_dropped(value):
    am = _registry().tool_call_to_action("twitter_whoami", {"action": value})
    assert am.model_dump(exclude_none=True)["twitter_whoami"] == {}


def test_a_different_action_value_still_fails():
    with pytest.raises(ValueError):
        _registry().tool_call_to_action("twitter_whoami", {"action": "post"})


def test_a_declared_action_field_is_never_dropped():
    am = _registry().tool_call_to_action("room_moderate", {"action": "moderate"})
    assert am.model_dump(exclude_none=True)["room_moderate"] == {"action": "moderate"}
