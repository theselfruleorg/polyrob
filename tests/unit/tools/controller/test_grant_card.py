"""030 WS-E1: the grant card — human approval requests, never raw JSON."""
from tools.controller.grant_card import render_grant_card, render_pending_preview


def test_payment_card_shows_amount_target_purpose_and_verbs():
    card = render_grant_card(
        "x402_invoice_x402_request",
        {"amount_usd": 12.5, "payer_contact": "acme@example.com",
         "purpose": "API integration work"},
        "tap-42", timeout_sec=300, grant_ttl_hours=24)
    assert "Amount: 12.5" in card
    assert "acme@example.com" in card
    assert "API integration work" in card
    assert "/approve tap-42" in card and "/reject tap-42" in card
    assert "300s" in card
    assert "one-shot grant" in card
    # Never a raw JSON dump.
    assert '{"' not in card


def test_paramless_card_still_renders_verbs():
    card = render_grant_card("self_env_restart_service", {}, "tap-7")
    assert card.startswith("🔐 Approval needed: self_env_restart_service")
    assert "/approve tap-7" in card


def test_extra_params_are_compact_and_bounded():
    card = render_grant_card(
        "shell_run", {"command": "x" * 500, "cwd": "/tmp", "timeout": 30},
        "tap-9")
    assert "…" in card          # long value truncated
    assert len(card) < 800


def test_card_never_raises_on_weird_params():
    card = render_grant_card("t", {"a": object()}, "tap-1")
    assert "Approval needed" in card


def test_pending_preview_is_action_first():
    p = render_pending_preview("x402_request", '{"amount_usd": 5, "purpose": "x"}')
    assert p.startswith("x402_request — ")
    assert not p.startswith("tool=")
    assert len(p) <= 160


# --- 030 WS-E3: resume-on-grant ---------------------------------------------

def test_decide_approved_wakes_the_originating_session():
    import asyncio

    from tools.controller.approval_queue import decide_tool_approval

    class _Row:
        payload = {"ask_kind": "tool_approval", "session_id": "sess-9",
                   "tool_name": "self_env_install_dep", "decision": None}

    class _Board:
        def get(self, rid):
            return _Row()

        def decide_ask(self, rid, *, user_id, approved, answer=None):
            return True, "ok"

    wakes = []

    class _Agent:
        def route_session(self, session_id):
            # 043 W10: model a SAME-process agent that OWNS the session (resident
            # here) — the only case an in-process self-wake is correct.
            from agents.task.session_route import LOCAL, SessionRoute
            return SessionRoute(status=LOCAL, orchestrator=object())

        async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
            wakes.append((session_id, user_id, text, metadata))
            return True

    async def _run():
        ok, msg = decide_tool_approval(_Board(), "tap-9", user_id="rob",
                                       approved=True, task_agent=_Agent())
        await asyncio.sleep(0)  # let the fire-and-forget task run
        return ok, msg

    ok, msg = asyncio.run(_run())
    assert ok is True and "approved" in msg
    assert wakes and wakes[0][0] == "sess-9"
    assert "one-shot grant" in wakes[0][2]


def test_decide_rejected_never_wakes():
    import asyncio

    from tools.controller.approval_queue import decide_tool_approval

    class _Row:
        payload = {"session_id": "sess-9", "tool_name": "t"}

    class _Board:
        def get(self, rid):
            return _Row()

        def decide_ask(self, rid, *, user_id, approved, answer=None):
            return True, "ok"

    wakes = []

    class _Agent:
        async def deliver_self_wake(self, *a, **k):
            wakes.append(a)
            return True

    async def _run():
        return decide_tool_approval(_Board(), "tap-9", user_id="rob",
                                    approved=False, task_agent=_Agent())

    asyncio.run(_run())
    assert wakes == []


def test_decide_without_agent_still_works_sync():
    from tools.controller.approval_queue import decide_tool_approval

    class _Board:
        def get(self, rid):
            return None

        def decide_ask(self, rid, *, user_id, approved, answer=None):
            return True, "ok"

    ok, _ = decide_tool_approval(_Board(), "tap-1", user_id="rob", approved=True)
    assert ok is True
