"""Item 7E/7H — HookPipeline direct unit coverage.

The pre/post/transform fail-mode engine extracted out of Controller. Tests target
``HookPipeline`` directly (no Controller) so the unit owns its own coverage after
the extraction. Semantics must match the legacy inline behaviour:
  - pre   open+raise -> allow (None);   closed+raise -> deny reason
  - transform open+raise -> keep result; closed+raise -> error ActionResult
  - post  open+raise -> swallow;         closed+raise -> propagate
  - hooks run in registration order; transform chains replacements.
"""
import logging

import pytest

from tools.controller.hooks import HookPipeline
from tools.controller.types import ActionResult


def _pipe() -> HookPipeline:
    return HookPipeline(logging.getLogger("hook-pipeline-test"))


def _boom(*a, **k):
    raise RuntimeError("hook bug")


# --- pre ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_no_hooks_allows():
    assert await _pipe().run_pre("write_file", {}, None) is None


@pytest.mark.asyncio
async def test_pre_open_failure_allows(caplog):
    p = _pipe()
    p.register_pre(_boom)  # default open
    with caplog.at_level(logging.ERROR):
        assert await p.run_pre("write_file", {}, None) is None
    assert any("hook.error" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_pre_closed_failure_denies():
    p = _pipe()
    p.register_pre(_boom, fail_mode="closed")
    assert "hook" in (await p.run_pre("write_file", {}, None) or "").lower()


@pytest.mark.asyncio
async def test_pre_runs_in_order_first_denial_wins():
    p = _pipe()
    p.register_pre(lambda n, pa, c: None)
    p.register_pre(lambda n, pa, c: "denied-2")
    p.register_pre(lambda n, pa, c: "denied-3")
    assert await p.run_pre("x", {}, None) == "denied-2"


# --- transform ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_transform_chains_replacements():
    p = _pipe()
    p.register_transform(lambda n, pa, r, c: ActionResult(extracted_content="first"))
    p.register_transform(lambda n, pa, r, c: ActionResult(extracted_content=r.extracted_content + "+second"))
    out = await p.run_transform("read", {}, ActionResult(extracted_content="orig"), None)
    assert out.extracted_content == "first+second"


@pytest.mark.asyncio
async def test_transform_open_failure_keeps_result():
    p = _pipe()
    p.register_transform(_boom)  # open
    r = ActionResult(extracted_content="safe")
    assert await p.run_transform("read", {}, r, None) is r


@pytest.mark.asyncio
async def test_transform_closed_failure_returns_error():
    p = _pipe()
    p.register_transform(_boom, fail_mode="closed")
    out = await p.run_transform("read", {}, ActionResult(extracted_content="safe"), None)
    assert out.error


# --- post --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_observes_and_open_swallows():
    p = _pipe()
    seen = []
    p.register_post(lambda n, pa, r, c: seen.append(n))
    p.register_post(_boom)  # open: swallowed
    await p.run_post("t", {}, ActionResult(extracted_content="x"), None)
    assert seen == ["t"]


@pytest.mark.asyncio
async def test_post_closed_propagates():
    p = _pipe()
    p.register_post(_boom, fail_mode="closed")
    with pytest.raises(RuntimeError):
        await p.run_post("t", {}, ActionResult(extracted_content="x"), None)
