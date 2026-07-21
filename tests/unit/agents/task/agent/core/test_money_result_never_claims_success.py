"""A money verb's empty result must never render as "Action completed successfully".

2026-07-19 fabrication incident. ``x402_fetch`` on a free-tier endpoint returned
an ActionResult with no error and no content (nothing was charged, nothing
failed). ``_pair_results_to_calls`` then applied its generic fallback and handed
the model the literal string ``"Action completed successfully"``. The agent read
that as payment confirmation and published "🚀 First x402 micro-transaction
completed!" to the public timeline and to its owner, having moved no money.

``tools/x402/service.py`` now states NO-PAYMENT explicitly, which fixes that one
call site. This is the framework-level backstop: for a MONEY tool (the
``core/tool_capabilities.py`` SSOT — x402_pay/x402_invoice/hyperliquid/polymarket)
an empty result must be reported as "no output", never as success, so the next
money tool that returns empty on a non-error path cannot recreate the incident.
"""
from tools.controller.types import ActionResult
from agents.task.agent.core.result_processing import _pair_results_to_calls


def _resolver(mapping):
    """mapping: tool_call_id -> (action_name, tool)."""
    return lambda tc_id: mapping.get(tc_id, (None, None))


def _pair_one(action_name, tool, **ar_kwargs):
    tcs = [{"id": "x", "name": action_name}]
    r = ActionResult(**ar_kwargs)
    r.tool_call_id = "x"
    paired = _pair_results_to_calls([r], tcs,
                                    source_for=_resolver({"x": (action_name, tool)}))
    return paired["x"]


def test_empty_money_result_does_not_claim_success():
    content, had_error = _pair_one("x402_fetch", "x402_pay")
    assert had_error is False
    assert "completed successfully" not in content.lower()
    assert "no output" in content.lower()


def test_empty_money_result_warns_against_reporting_success():
    """The model must be told explicitly that this is not confirmation — the
    incident showed a bare neutral string is still read as success under
    completion pressure."""
    content, _ = _pair_one("x402_pay", "x402_pay")
    low = content.lower()
    assert "not confirmation" in low
    assert "verify" in low


def test_money_result_with_real_content_is_untouched():
    content, had_error = _pair_one("x402_fetch", "x402_pay",
                                   extracted_content="[paid $0.2500 to 0xR, tx 0xabc]")
    assert had_error is False
    assert content == "[paid $0.2500 to 0xR, tx 0xabc]"


def test_money_error_still_reports_the_error():
    content, had_error = _pair_one("x402_fetch", "x402_pay",
                                   error="payment blocked: daily cap reached")
    assert had_error is True
    assert "daily cap reached" in content


def test_non_money_empty_result_keeps_the_legacy_string():
    """Scoped change: only money tools are demoted, so every other tool's
    empty-result rendering stays byte-identical."""
    content, had_error = _pair_one("read_file", "filesystem")
    assert had_error is False
    assert content == "Action completed successfully"


def test_no_resolver_keeps_the_legacy_string():
    """source_for=None (legacy/non-native call sites) must be unchanged."""
    tcs = [{"id": "x", "name": "x402_fetch"}]
    r = ActionResult()
    r.tool_call_id = "x"
    paired = _pair_results_to_calls([r], tcs, source_for=None)
    assert paired["x"] == ("Action completed successfully", False)


def test_done_result_is_still_task_complete():
    content, had_error = _pair_one("done", "x402_pay", is_done=True)
    assert had_error is False
    assert content == "Task marked as complete"
