"""098 TS-16: a hook ``fail_mode`` is "open" or "closed" — anything else fails
at registration (never fails OPEN on a guardrail); a keyed hook replaces its
earlier registration; the same callable twice is not added twice."""
import asyncio

import pytest

from tools.controller.hooks import HookPipeline


@pytest.mark.parametrize("bad", ["close", "Closed", "", None, "deny"])
def test_unknown_fail_mode_rejected(bad):
    pipe = HookPipeline()
    with pytest.raises(ValueError):
        pipe.register_pre(lambda *a: None, bad)
    with pytest.raises(ValueError):
        pipe.register_post(lambda *a: None, bad)
    with pytest.raises(ValueError):
        pipe.register_transform(lambda *a: None, bad)
    assert pipe.pre == pipe.post == pipe.transform == []


def test_keyed_registration_replaces():
    pipe = HookPipeline()
    first = lambda a, p, c: "first"  # noqa: E731
    other = lambda a, p, c: None  # noqa: E731
    second = lambda a, p, c: "second"  # noqa: E731
    pipe.register_pre(first, "closed", key="gate")
    pipe.register_pre(other, "open")
    pipe.register_pre(second, "closed", key="gate")
    assert [h for h, _ in pipe.pre] == [second, other]
    assert asyncio.run(pipe.run_pre("x", {}, None)) == "second"


def test_duplicate_registration_is_not_added_twice():
    pipe = HookPipeline()
    hook = lambda a, p, c: None  # noqa: E731
    pipe.register_pre(hook, "closed")
    pipe.register_pre(hook, "closed")
    assert len(pipe.pre) == 1


def test_controller_register_validates_fail_mode():
    from tools.controller.service import Controller
    c = Controller.__new__(Controller)
    with pytest.raises(ValueError):
        c.register_pre_tool_call_hook(lambda *a: None, fail_mode="Closed")
