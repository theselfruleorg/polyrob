"""O9: `/wallet` shows used today, left today and the ask-above line, and an
unreadable read renders `unavailable(<reason>)`, never $0."""
from core.wallet.view import WalletView, _headroom, render_wallet


class _Gate:
    per_tx_cap_usd = 100.0
    daily_cap_usd = 400.0

    def rolling_24h_spend_usd(self):
        return 123.4


class _BrokenGate(_Gate):
    def rolling_24h_spend_usd(self):
        raise OSError("disk")


def _render(gate):
    caps = {"per_tx_usd": 100.0, "daily_usd": 400.0, **_headroom(gate)}
    return render_wallet(WalletView(owner="o", state="ready", caps=caps))


def test_used_left_and_ceiling_render(monkeypatch):
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "25")
    out = _render(_Gate())
    assert "Used today (24h): $123.40; left today: $276.60" in out
    assert "I ask you above $25.00" in out


def test_unreadable_spend_is_unavailable_not_zero():
    out = _render(_BrokenGate())
    assert "Used today (24h): unavailable(spend ledger unreadable: OSError)" in out
    assert "$0.00" not in out


def test_unaccounted_submissions_say_what_they_mean():
    view = WalletView(owner="o", state="ready",
                      unaccounted_submissions=[{"chain": "base", "tx_hash": "0xabc"}])
    out = render_wallet(view)
    assert "could not confirm the result" in out and "polyrob" not in out
