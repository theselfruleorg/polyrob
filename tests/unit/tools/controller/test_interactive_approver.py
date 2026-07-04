"""P0 Task 9 — InteractiveCLIApprover (injectable, cancellation-safe)."""
import time

import pytest

from tools.controller.approval import make_approval_hook, get_approval_provider
from tools.controller.approval_interactive import InteractiveCLIApprover


@pytest.mark.asyncio
async def test_approve_allows():
    prov = InteractiveCLIApprover(input_fn=lambda prompt: "y")
    assert await prov.request("git_push", {"remote": "origin"}, None) is True


@pytest.mark.asyncio
async def test_deny_denies():
    prov = InteractiveCLIApprover(input_fn=lambda prompt: "n")
    assert await prov.request("git_push", {}, None) is False


def test_registered_and_resolvable():
    assert isinstance(get_approval_provider("interactive_cli"), InteractiveCLIApprover)


@pytest.mark.asyncio
async def test_hook_allows_on_yes():
    hook = make_approval_hook(InteractiveCLIApprover(input_fn=lambda p: "yes"), ["git_push"])
    assert await hook("git_push", {"x": 1}, None) is None


@pytest.mark.asyncio
async def test_hook_denies_on_no():
    hook = make_approval_hook(InteractiveCLIApprover(input_fn=lambda p: "no"), ["git_push"])
    reason = await hook("git_push", {}, None)
    assert reason and "denied" in reason


@pytest.mark.asyncio
async def test_hook_denies_on_timeout():
    def _block(prompt):
        time.sleep(5)
        return "y"
    hook = make_approval_hook(InteractiveCLIApprover(input_fn=_block), ["git_push"], timeout=0.2)
    reason = await hook("git_push", {}, None)
    assert reason and "timeout" in reason.lower()


@pytest.mark.asyncio
async def test_ungated_action_passes():
    hook = make_approval_hook(InteractiveCLIApprover(input_fn=lambda p: "n"), ["git_push"])
    assert await hook("git_status", {}, None) is None
