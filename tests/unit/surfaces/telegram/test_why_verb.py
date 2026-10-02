"""O10: `/why` lists the last typed gate refusals, reason verbatim, newest
first; an absent store is `unavailable(...)`, never "no refusals"."""
import time

from core.event_kinds import TOOL_DENIED
from core.event_log import TelemetryEventLog


def _seed(tmp_path, monkeypatch):
    db = tmp_path / "telemetry_events.db"
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(db))
    log = TelemetryEventLog(str(db))
    now = time.time()
    log.record(TOOL_DENIED, user_id="owner", source="gate", ts=now - 120,
               attrs={"reason": "money_gate", "tool": "defi_trade",
                      "detail": "autonomous turn may not sign"})
    log.record(TOOL_DENIED, user_id="", source="gate", ts=now - 60,
               attrs={"reason": "pause", "tool": "email", "detail": ""})
    # the untyped controller emit and another tenant's row stay out
    log.record(TOOL_DENIED, user_id="owner", source="controller", ts=now,
               attrs={"reason": "prose veto", "tool": "x"})
    log.record(TOOL_DENIED, user_id="other", source="gate", ts=now,
               attrs={"reason": "forged_turn", "tool": "y"})


def test_lists_gate_refusals_newest_first(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    from surfaces.telegram.why_ops import why_reply
    out = why_reply("owner", str(tmp_path), [])
    assert out.startswith("Last 2 refusal(s)")
    assert out.index("email — pause") < out.index("defi_trade — money_gate")
    assert "autonomous turn may not sign" in out
    assert "prose veto" not in out and "forged_turn" not in out


def test_limit_and_bad_arg(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    from surfaces.telegram.why_ops import why_reply
    assert why_reply("owner", str(tmp_path), ["1"]).startswith("Last 1 refusal(s)")
    assert why_reply("owner", str(tmp_path), ["x"]).startswith("Usage")


def test_absent_store_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "none.db"))
    from surfaces.telegram.why_ops import why_reply
    out = why_reply("owner", str(tmp_path), [])
    assert out.startswith("Refusals: unavailable(")
    assert not (tmp_path / "none.db").exists()
