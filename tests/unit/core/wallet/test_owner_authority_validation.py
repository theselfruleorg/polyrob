"""Owner-authority validation (2026-09-27): holes found in the owner-direct rule.

The approval hook stamped an owner grant for ANY provider that answered True —
including ``AutoApprover`` / ``AutoNotifyApprover``, which answer True with no
human asked. A grant lifts the autonomous ceiling (tx_guard step 9), so only a
provider whose True IS the owner's decision may mint one.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

OWNER = "owner-1"
TO = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"
HOLDER = "0x2222222222222222222222222222222222222222"
AMOUNT = 10 ** 18


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda: OWNER)
    monkeypatch.setenv("DEFI_AUTONOMOUS_TURN_TRADING", "true")


def _ctx(**kw):
    base = dict(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                session_id="s-owner", metadata={})
    base.update(kw)
    return SimpleNamespace(**base)


# -- 1. only the owner's decision mints a grant ---------------------------------

@pytest.mark.parametrize("provider_name", ["auto", "auto_notify"])
def test_an_automatic_approver_mints_no_owner_grant(provider_name):
    from tools.controller.approval import get_approval_provider, make_approval_hook
    ctx = _ctx()
    hook = make_approval_hook(get_approval_provider(provider_name),
                              ["defi_trade_transfer"], timeout=5)
    assert asyncio.run(hook("defi_trade_transfer", {"max_spend_usd": 2800.0}, ctx)) is None
    assert tx_guard.OWNER_GRANT_KEY not in ctx.metadata, (
        f"{provider_name!r} approves with no human asked; its True must not lift "
        f"the autonomous ceiling")


def test_the_owner_queue_decides_as_the_owner():
    import tools.controller.approval_queue  # noqa: F401 — registers owner_queue
    from tools.controller.approval import ApprovalProvider
    from tools.controller.approval_queue import OwnerQueueApprover
    assert OwnerQueueApprover.decides_as_owner is True
    assert ApprovalProvider.decides_as_owner is False


def test_an_owner_deciding_provider_mints_the_grant():
    from tools.controller.approval import ApprovalProvider, make_approval_hook

    class _Owner(ApprovalProvider):
        decides_as_owner = True

        async def request(self, action, params, ctx):
            return True

    ctx = _ctx()
    hook = make_approval_hook(_Owner(), ["defi_trade_transfer"], timeout=5)
    asyncio.run(hook("defi_trade_transfer", {"max_spend_usd": 2800.0}, ctx))
    assert ctx.metadata[tx_guard.OWNER_GRANT_KEY] == {"approved": True, "max_spend_usd": 2800.0}
