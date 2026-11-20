"""Audit 2026-11-21 M-4 — the outcome of a code-run money act is one of done, failed or
unknown. A broadcast whose end is unknown is never "failed" (a retry could pay twice), and a
reverted act is never "done" (the debt it was to pay would be dropped)."""
from types import SimpleNamespace

import pytest

from tools.autonomous_act import DONE, FAILED, UNKNOWN, outcome, verdict

TX = "0x" + "3" * 64


def _r(content="", error=None, metadata=None):
    return SimpleNamespace(extracted_content=content, error=error, metadata=metadata)


@pytest.mark.parametrize("res,want", [
    (_r(f"swap\n  RESULT: CONFIRMED\n  tx: {TX}"), DONE),
    (_r(f"  RESULT: CONFIRMED (slot 9)\n  sig: x"), DONE),
    (_r(f"  RESULT: REVERTED ON-CHAIN — it did NOT happen, but gas was spent.\n  tx: {TX}"),
     FAILED),
    (_r("  RESULT: EXPIRED — the swap never landed"), FAILED),
    (_r(f"  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout.\n  tx: {TX}"), UNKNOWN),
    (_r(error="broadcast outcome unknown: timeout. The transaction may have landed — look it "
              "up on the chain before any retry; do not send again."), UNKNOWN),
    (_r(error="broadcast failed: nonce too low — nothing was sent"), FAILED),
    (_r(error="refused: the wallet is paused"), FAILED),
    (_r("NOT SENT: over the cap"), FAILED),
    # a live result that names no end is never booked done
    (_r("something happened"), UNKNOWN),
    # an explicit outcome in the metadata wins over the words
    (_r("RESULT: CONFIRMED", metadata={"outcome": "unknown"}), UNKNOWN),
    (_r("no words", metadata={"outcome": "reverted"}), FAILED),
    (_r("no words", metadata={"outcome": "confirmed"}), DONE),
])
def test_a_live_result_maps_to_one_outcome(res, want):
    assert outcome(res, live=True) == want
    assert verdict(res, live=True)[0] is (want == DONE)


def test_a_dry_run_is_done_only_when_the_guard_allowed_it():
    assert outcome(_r("x", metadata={"allowed": True}), live=False) == DONE
    assert outcome(_r("DRY RUN ok"), live=False) == DONE
    assert outcome(_r("x", metadata={"allowed": False}), live=False) == FAILED
