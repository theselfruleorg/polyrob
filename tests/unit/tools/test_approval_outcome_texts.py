"""095 W1b: approval outcomes are typed; /pending only when an owner ask exists."""
import types

import pytest

from tools.controller.approval import (ApprovalOutcome, DenyByDefaultApprover,
                                       make_approval_hook, not_created, outcome_text)

CASES = [
    (ApprovalOutcome("pending", pending_id="tap-1"), True),
    (ApprovalOutcome("timeout", pending_id="tap-2"), True),
    (ApprovalOutcome("timeout"), False),
    (ApprovalOutcome("denied_by_owner"), False),
    (ApprovalOutcome("crashed", reason="provider crashed: X"), False),
    (ApprovalOutcome("pending"), False),
] + [(not_created(r), False) for r in (
    "forged", "leaf", "autonomous", "tainted", "store_down", "no_provider",
    "money_owner", "busy", "duplicate", "closed", "")]


@pytest.mark.parametrize("outcome,has_ask", CASES, ids=lambda c: repr(c))
def test_pending_named_iff_an_ask_exists(outcome, has_ask):
    text = outcome_text(outcome, "write_file")
    assert ("/pending" in text) is has_ask
    if has_ask:
        assert outcome.pending_id in text
    assert "write_file" in text


def test_outcome_is_falsy_unless_approved():
    assert ApprovalOutcome("approved")
    for kind in ("pending", "denied_by_owner", "timeout", "not_created", "crashed"):
        assert not ApprovalOutcome(kind)


def test_owner_no_says_do_not_retry():
    text = outcome_text(ApprovalOutcome("denied_by_owner"), "write_file")
    assert "owner declined" in text and "do not retry" in text


def test_not_created_names_why():
    assert "correspondent" in outcome_text(not_created("tainted"), "write_file")
    assert "APPROVAL_PROVIDER" in outcome_text(not_created("no_provider"), "write_file")
    assert "store" in outcome_text(not_created("store_down"), "write_file")


@pytest.mark.asyncio
async def test_deny_by_default_never_points_at_pending():
    hook = make_approval_hook(DenyByDefaultApprover(), {"write_file"})
    text = await hook("write_file", {}, types.SimpleNamespace(user_id="u", session_id="s"))
    assert text and "/pending" not in text and "APPROVAL_PROVIDER" in text


@pytest.mark.asyncio
async def test_bare_false_from_the_repl_is_the_owners_no():
    from tools.controller.approval_interactive import InteractiveCLIApprover
    prov = InteractiveCLIApprover(input_fn=lambda p: "d")
    text = await make_approval_hook(prov, {"write_file"})("write_file", {}, None)
    assert "owner declined" in text and "/pending" not in text


@pytest.mark.asyncio
async def test_bare_false_from_an_unknown_provider_is_not_an_ask():
    class _No:
        async def request(self, *a, **k):
            return False
    text = await make_approval_hook(_No(), {"write_file"})("write_file", {}, None)
    assert text and "/pending" not in text
