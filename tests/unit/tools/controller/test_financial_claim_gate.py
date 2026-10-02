"""022 — the send-time gate for financial success claims.

A claim with no settlement is refused by name; a claim with a matching
settlement goes; text with no claim is untouched; and every outbound verb of
every outbound tool is in the gate's table (the ratchet).
"""
import inspect
import time
import types

import pytest

import tools.controller.financial_claim_gate as gate
from core.rails import financial_claims as fc
from core.rails.financial_claims import (
    RECEIVE, SPEND, RecordsRead, SettledRecord, detect_claim, verify_claim,
)

# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

CLAIMS = [
    "🚀 First x402 micro-transaction completed! Just executed my first "
    "autonomous agent payment via x402 protocol to Quicknode RPC ($1 cap)",
    "I paid $5 to the Quicknode endpoint.",
    "We earned $12.50 from our first customer today",
    "Invoice abc123 was paid.",
    "Received a payment of 3 USDC.",
    "I just sent 0.01 ETH to the treasury.",
    "Payment completed: $2.00 to example.com",
]

NOT_CLAIMS = [
    "I tried to pay $1 for the RPC call; it did not complete.",
    "The payment failed with a 402.",
    "Payment pending — waiting for settlement.",
    "I will pay $5 once you approve.",
    "Did the $5 invoice get paid?",
    "I sent you the invoice for the design work.",
    "We earned $0 so far.",
    "Received your email, thanks — I'll read it tonight.",
    "We settled on the blue logo.",
    "Here is the thread about agent payments on x402.",
    "I created an invoice for $5: https://pay.example/abc",
    "No payment was made.",
]


@pytest.mark.parametrize("text", CLAIMS)
def test_claims_are_detected(text):
    assert detect_claim(text) is not None, text


@pytest.mark.parametrize("text", NOT_CLAIMS)
def test_honest_or_unrelated_text_is_not_a_claim(text):
    assert detect_claim(text) is None, text


def test_direction_and_amounts():
    c = detect_claim("I paid $5 to the endpoint.")
    assert c.direction == SPEND and c.usd_amounts == (5.0,)
    c = detect_claim("We earned 250 cents today.")
    assert c.direction == RECEIVE and c.usd_amounts == (2.5,)


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

NOW = 1_800_000_000.0


def _rec(direction, amount, age_sec=60):
    return SettledRecord(direction=direction, amount_usd=amount, ts=NOW - age_sec)


def test_claim_without_settlement_is_refused_by_name():
    claim = detect_claim("I paid $5 to the endpoint.")
    refusal = verify_claim(claim, RecordsRead(), now=NOW)
    assert refusal and refusal.startswith(f"refused ({fc.REFUSAL_SLUG})")
    assert "x402_wallet_status" in refusal and "Nothing was sent" in refusal


def test_claim_with_matching_settlement_is_allowed():
    claim = detect_claim("I paid $5 to the endpoint.")
    assert verify_claim(claim, RecordsRead(records=[_rec(SPEND, 4.98)]), now=NOW) is None


def test_amount_mismatch_is_refused():
    claim = detect_claim("I paid $50 to the endpoint.")
    assert verify_claim(claim, RecordsRead(records=[_rec(SPEND, 5.0)]), now=NOW)


def test_direction_is_respected():
    claim = detect_claim("We earned $5 today.")
    assert verify_claim(claim, RecordsRead(records=[_rec(SPEND, 5.0)]), now=NOW)
    assert verify_claim(claim, RecordsRead(records=[_rec(RECEIVE, 5.0)]), now=NOW) is None


def test_amountless_claim_needs_a_fresh_record():
    claim = detect_claim("First x402 micro-transaction completed!")
    old = RecordsRead(records=[_rec(SPEND, 1.0, age_sec=3 * 24 * 3600)])
    assert verify_claim(claim, old, now=NOW)
    fresh = RecordsRead(records=[_rec(SPEND, 1.0, age_sec=600)])
    assert verify_claim(claim, fresh, now=NOW) is None


def test_unreadable_store_blocks_and_says_so():
    claim = detect_claim("I paid $5 to the endpoint.")
    refusal = verify_claim(claim, RecordsRead(unreadable=["the wallet audit log"]), now=NOW)
    assert refusal and "could not read the wallet audit log" in refusal


# --------------------------------------------------------------------------
# the hook
# --------------------------------------------------------------------------

@pytest.fixture
def records(monkeypatch):
    box = {"read": RecordsRead()}
    calls = []

    async def _read(user_id):
        calls.append(user_id)
        return box["read"]

    monkeypatch.setattr(gate, "read_settled_records", _read)
    monkeypatch.delenv("FINANCIAL_CLAIM_GATE_ENABLED", raising=False)
    box["calls"] = calls
    return box


def _ctx():
    return types.SimpleNamespace(user_id="u1", session_id="s1")


@pytest.mark.asyncio
async def test_hook_refuses_an_unbacked_tweet(records):
    hook = gate.make_financial_claim_hook(types.SimpleNamespace(user_id="u1"))
    reason = await hook("twitter_post", {"text": "I paid $1 for my first x402 call!"}, _ctx())
    assert reason and fc.REFUSAL_SLUG in reason
    assert records["calls"] == ["u1"]


@pytest.mark.asyncio
async def test_hook_allows_a_backed_claim(records):
    records["read"] = RecordsRead(records=[SettledRecord(SPEND, 1.0, time.time() - 30)])
    hook = gate.make_financial_claim_hook(types.SimpleNamespace(user_id="u1"))
    assert await hook("message", {"text": "I paid $1 for my first x402 call."}, _ctx()) is None


@pytest.mark.asyncio
async def test_hook_leaves_non_claims_and_non_outbound_untouched(records):
    hook = gate.make_financial_claim_hook(types.SimpleNamespace(user_id="u1"))
    assert await hook("message", {"text": "Morning! Three goals done."}, _ctx()) is None
    assert await hook("filesystem_write_file", {"text": "I paid $5."}, _ctx()) is None
    assert records["calls"] == []   # no claim -> no store read at all


@pytest.mark.asyncio
async def test_thread_checks_every_post(records):
    hook = gate.make_financial_claim_hook(types.SimpleNamespace(user_id="u1"))
    reason = await hook("twitter_thread",
                        {"texts": ["A thread about agents.", "We earned $40 this week."]},
                        _ctx())
    assert reason and fc.REFUSAL_SLUG in reason


@pytest.mark.asyncio
async def test_kill_switch(records, monkeypatch):
    monkeypatch.setenv("FINANCIAL_CLAIM_GATE_ENABLED", "false")
    hook = gate.make_financial_claim_hook(types.SimpleNamespace(user_id="u1"))
    assert await hook("twitter_post", {"text": "I paid $1 today."}, _ctx()) is None


def test_sqlite_timestamps_parse():
    assert gate._epoch("2026-09-23 10:00:00") == pytest.approx(1790157600.0)
    assert gate._epoch(12.5) == 12.5
    assert gate._epoch("garbage") == 0.0


@pytest.mark.asyncio
async def test_controller_registers_the_gate(tmp_path, records):
    import agents.task.agent.service  # noqa: F401 — controller<->orchestrator cycle
    from tools.controller.service import Controller

    orch = types.SimpleNamespace(session_id="s1", user_id="u1", workspace_dir=str(tmp_path))
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    c = Controller(container=container, orchestrator=orch)
    reason = await c._run_pre_tool_call_hooks(
        "email_send", {"to": "a@b.c", "subject": "Update", "body": "We earned $9 today."},
        _ctx())
    assert reason and fc.REFUSAL_SLUG in reason


# --------------------------------------------------------------------------
# the ratchet: every outbound verb passes the gate
# --------------------------------------------------------------------------

#: tool_id -> class, for every tool whose catalog permissions post or send.
_OUTBOUND_TOOL_CLASSES = {
    "twitter": ("polyrob_x.twitter_tool", "TwitterTool"),     # the X pack (067 P3b)
    "email": ("tools.email_tool", "EmailTool"),
    "x_browser": ("polyrob_x.x_browser.tool", "XBrowserTool"),
}

#: Param fields that carry words to another person.
_TEXT_FIELDS = {"text", "texts", "body", "subject", "message", "content", "caption",
                "comment", "poll_options", "reply", "note", "title"}


def _importable(tool_id: str) -> bool:
    import importlib.util
    top = _OUTBOUND_TOOL_CLASSES[tool_id][0].split(".", 1)[0]
    return importlib.util.find_spec(top) is not None


def test_every_posting_tool_is_enumerated():
    from core.tool_capabilities import TOOL_PERMISSIONS
    posting = {t for t, perms in TOOL_PERMISSIONS.items()
               if {"social.post", "email.send"} & set(perms)}
    # A pack tool whose pack is not installed has no permission row here.
    assert posting == {t for t in _OUTBOUND_TOOL_CLASSES if _importable(t)}, (
        "a tool gained social.post/email.send: add it to _OUTBOUND_TOOL_CLASSES "
        "and its text verbs to OUTBOUND_TEXT_FIELDS")


def test_every_text_bearing_outbound_verb_is_gated():
    import importlib
    missing = []
    for tid, (mod, cls_name) in _OUTBOUND_TOOL_CLASSES.items():
        if not _importable(tid):
            continue
        cls = getattr(importlib.import_module(mod), cls_name)
        for name, member in inspect.getmembers(cls):
            model = getattr(member, "_param_model", None)
            if model is None:
                continue
            words = set(model.model_fields) & _TEXT_FIELDS
            if not words:
                continue
            covered = set(gate.OUTBOUND_TEXT_FIELDS.get(name, ()))
            if not words <= covered:
                missing.append(f"{name}: {sorted(words - covered)}")
    assert not missing, f"outbound verbs not covered by the 022 gate: {missing}"


def test_controller_speech_verbs_are_gated():
    from tools.controller.views import MessageTargetAction, SendMessageAction
    assert "text" in SendMessageAction.model_fields
    assert "text" in MessageTargetAction.model_fields
    assert gate.OUTBOUND_TEXT_FIELDS["message"] == ("text",)
    assert gate.OUTBOUND_TEXT_FIELDS["send_message"] == ("text",)
