"""Item 7E — minimal ApprovalProvider seam.

A pre-tool-call hook factory gates a configured set of action names through an
ApprovalProvider. Default AutoApprover allows; DenyByDefaultApprover denies; a
provider timeout/error denies (the hook is registered fail_mode="closed"). Actions
NOT in the gated set always pass (hook returns None).
"""
import asyncio

import pytest

from tools.controller.approval import (
    ApprovalProvider,
    AutoApprover,
    DenyByDefaultApprover,
    get_approval_provider,
    make_approval_hook,
)


@pytest.mark.asyncio
async def test_auto_approver_allows_listed_tool():
    hook = make_approval_hook(AutoApprover(), {"run_code"})
    assert await hook("run_code", {}, None) is None  # allowed


@pytest.mark.asyncio
async def test_deny_provider_denies_listed_tool():
    hook = make_approval_hook(DenyByDefaultApprover(), {"run_code"})
    reason = await hook("run_code", {}, None)
    assert reason and "run_code" in reason


@pytest.mark.asyncio
async def test_unlisted_tool_always_allowed():
    hook = make_approval_hook(DenyByDefaultApprover(), {"run_code"})
    assert await hook("read_file", {}, None) is None  # not gated -> allow


@pytest.mark.asyncio
async def test_timeout_denies():
    class _SlowApprover(ApprovalProvider):
        async def request(self, action_name, params, context):
            await asyncio.sleep(1.0)  # timeout=0.1 fires long before this
            return True

    hook = make_approval_hook(_SlowApprover(), {"run_code"}, timeout=0.1)
    reason = await hook("run_code", {}, None)
    assert reason and "timeout" in reason  # real asyncio.wait_for timeout -> denied


def test_get_provider_by_name():
    assert isinstance(get_approval_provider("auto"), AutoApprover)
    assert isinstance(get_approval_provider("deny"), DenyByDefaultApprover)


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        get_approval_provider("nope")


def test_the_owners_tap_on_a_reveal_grants_the_operators_fee_cap(monkeypatch):
    """The rig (CW2, 2026-11-20): a reveal names no ``max_spend_usd`` (its cap is
    AGENT_NFT_REVEAL_MAX_GAS_USD), so the owner's tap stamped no grant and the guard queued
    the approved reveal again: it never landed. Any other verb with no bound gets no grant."""
    from types import SimpleNamespace

    from tools.controller.approval import _stamp_owner_grant
    from core.wallet.tx_guard import OWNER_GRANT_KEY
    monkeypatch.setenv("AGENT_NFT_REVEAL_MAX_GAS_USD", "0.6")
    ctx = SimpleNamespace(metadata={})
    _stamp_owner_grant(ctx, {"chain": "robinhood"}, action="agent_nft_collection_reveal")
    assert ctx.metadata[OWNER_GRANT_KEY] == {"approved": True, "max_spend_usd": 0.6}
    other = SimpleNamespace(metadata={})
    _stamp_owner_grant(other, {"chain": "robinhood"}, action="agent_nft_collection_mint")
    assert OWNER_GRANT_KEY not in other.metadata
