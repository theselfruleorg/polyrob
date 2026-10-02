"""CR-L23: since 2026-09-18 an owner-approved budget.wallet_per_tx_usd can RAISE
the per-transaction ceiling (clamped to the daily cap). The /wallet autonomous
copy must not tell the owner it is env-only or not settable from chat."""
from surfaces.telegram.owner_ops import _set_autonomous_ceiling


def test_usage_names_the_per_tx_pref_not_env_only():
    out = _set_autonomous_ceiling([], "owner-1", None)
    assert "env-only" not in out
    assert "budget.wallet_per_tx_usd" in out


def test_clamp_note_names_the_pref(monkeypatch, tmp_path):
    import core.prefs as prefs
    import core.runtime_paths as rp
    from core.wallet import tx_guard
    monkeypatch.setattr(rp, "prefs_home_dir", lambda: str(tmp_path))
    monkeypatch.setattr(prefs, "write_preference", lambda *a, **k: None)
    monkeypatch.setattr(tx_guard, "autonomous_max_usd", lambda *a, **k: 5.0)
    out = _set_autonomous_ceiling(["50"], "owner-1", None)
    assert "not settable" not in out and "by design" not in out
    assert "budget.wallet_per_tx_usd" in out
