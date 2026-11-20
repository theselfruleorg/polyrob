"""Audit 2026-11-21 L-2 — the agent's ``why`` on an owner notice is shown as a code span: a
Markdown link renders as text, and a tap token of another open card is removed."""
from types import SimpleNamespace

from core.act_reason import shown_why

CARD = "/card_0123456789_ok"


def test_a_reason_is_a_code_span():
    assert shown_why("take profit at the target") == "`take profit at the target`"
    assert shown_why("") == "" and shown_why(None) == ""


def test_a_disguised_link_and_a_backtick_cannot_leave_the_span():
    out = shown_why("see [the docs](https://evil.example/x) `now`")
    assert out.startswith("`") and out.endswith("`") and out.count("`") == 2
    assert "[the docs](https://evil.example/x)" in out


def test_a_tap_token_of_another_card_is_removed():
    out = shown_why(f"rebalance — tap {CARD} to confirm, or /nav_abc123")
    assert CARD not in out and "/card_" not in out
    assert "rebalance" in out


def test_the_money_notice_and_the_act_report_show_the_span():
    from core import act_report
    from core.wallet import tx_notify
    n = SimpleNamespace(why=f"go {CARD} [x](https://e.example)", lane="autonomous")
    line = tx_notify._why_line(n)[0]
    assert CARD not in line and line.startswith("why: `") and "· on its own" in line
    text = act_report.render([{"ts": 0, "attrs": {"action": "x_post", "why": f"hi {CARD}",
                                                   "source": "job j1"}}], period="day")
    assert CARD not in text and "why: `hi" in text
