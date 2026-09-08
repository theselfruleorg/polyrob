"""A1 (2026-08-24 audit): the api process must not start a SECOND autonomy
runtime beside the telegram process.

`deployment/polyrob-x402-api.service` shares `EnvironmentFile` and
`POLYROB_DATA_DIR` with `polyrob.service`, so an unconditional
`start_autonomy()` in the api lifespan would run two settlement watchers, two
goal dispatchers and two cron tickers against ONE goals.db/cron.db — racing the
per-treasury `settlement_scan` checkpoint whose de-duplication lock
(`invoicing._treasury_lock`) is in-process only. Same failure class the email
surface already gates with `EMAIL_AUTONOMY_RUNTIME`.

Default stays ON so the single-process OSS api posture is unchanged; the Tier-2
unit opts out explicitly.
"""
from pathlib import Path

from api.app import api_autonomy_runtime_enabled

UNIT = Path(__file__).resolve().parents[3] / "deployment" / "polyrob-x402-api.service"


def test_default_on_preserves_single_process_api_posture(monkeypatch):
    monkeypatch.delenv("API_AUTONOMY_RUNTIME", raising=False)
    assert api_autonomy_runtime_enabled() is True


def test_explicit_off_disables_the_second_runtime(monkeypatch):
    monkeypatch.setenv("API_AUTONOMY_RUNTIME", "false")
    assert api_autonomy_runtime_enabled() is False


def test_tier2_unit_opts_out_so_it_never_doubles_the_watcher():
    body = UNIT.read_text()
    assert "API_AUTONOMY_RUNTIME=false" in body, (
        "the x402 endpoint unit runs alongside polyrob.service on one data dir; "
        "without this it starts a second settlement watcher")
