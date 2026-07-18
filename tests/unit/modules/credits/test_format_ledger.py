from modules.credits.unified_ledger import format_ledger


def _led(**over):
    led = {
        "user_id": "rob", "window_days": 1,
        "treasury": {"income_usd": 0.0, "spend_usd": 0.0, "pending_usd": 2.0,
                     "pending_count": 1, "balance_usd": 10.0, "net_usd": 0.0,
                     "available": True},
        "runtime": {"spend_window_usd": 2.47, "spend_total_usd": 13.97,
                    "calls_window": 100, "calls_total": 561,
                    "provider_balance_usd": -0.17, "available": True},
        "costs_available": True, "inbound_available": True, "wallet_metering": "on",
    }
    led.update(over)
    return led


def test_renders_two_separate_statements():
    out = format_ledger(_led())
    assert "Treasury" in out and "Runtime cost" in out
    assert "income" in out and "spend" in out
    # the merge must be gone: no single "spent: $X total" line summing both
    assert "16.44" not in out          # 2.47 + 13.97 must never appear
    # treasury net_usd=0.0 renders SIGNED (":+.4f") — "$0.00" would actually
    # match the (also-zero) income/spend lines, not net at all; assert the
    # real rendered net figure so this guards what it claims to guard.
    assert "net:" in out and "+0.0000" in out          # treasury net


def test_runtime_has_no_net():
    out = format_ledger(_led())
    runtime_section = out.split("Runtime cost")[1]
    assert "net" not in runtime_section.lower()


def test_balance_omitted_when_none():
    led = _led()
    led["runtime"]["provider_balance_usd"] = None
    led["treasury"]["balance_usd"] = None
    out = format_ledger(led)
    assert "balance" not in out.lower()


def test_treasury_balance_shown_when_runtime_balance_none():
    """The two balance guards (:256-257 treasury, :263-264 runtime) are
    independent — cover treasury-present/runtime-absent so one guard can't
    silently swallow the other's balance."""
    led = _led()
    led["runtime"]["provider_balance_usd"] = None
    out = format_ledger(led)
    assert "balance:  $10.0000" in out          # treasury balance still renders
    runtime_section = out.split("Runtime cost")[1]
    assert "balance" not in runtime_section.lower()          # omitted, not $0.00
    assert "$0.00" not in runtime_section


def test_runtime_balance_shown_when_treasury_balance_none():
    """Mirror of the above: runtime-present/treasury-absent."""
    led = _led()
    led["treasury"]["balance_usd"] = None
    out = format_ledger(led)
    treasury_section = out.split("Treasury")[1].split("Runtime cost")[0]
    # omitted, not fabricated as $0.00 — note income/spend legitimately render
    # "$0.0000" in this fixture, so assert no "balance:" LINE at all rather
    # than a blanket "$0.00 not in section" (that would false-fail on them).
    assert "balance" not in treasury_section.lower()
    assert "balance:  $-0.1700" in out          # runtime balance still renders


def test_availability_note_still_appended():
    led = _led(costs_available=False)
    led["runtime"]["available"] = False
    out = format_ledger(led)
    assert "⚠" in out
