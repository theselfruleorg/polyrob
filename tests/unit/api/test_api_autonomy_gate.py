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
from api.app import api_autonomy_runtime_enabled


def test_default_on_preserves_single_process_api_posture(monkeypatch):
    monkeypatch.delenv("API_AUTONOMY_RUNTIME", raising=False)
    assert api_autonomy_runtime_enabled() is True


def test_explicit_off_disables_the_second_runtime(monkeypatch):
    monkeypatch.setenv("API_AUTONOMY_RUNTIME", "false")
    assert api_autonomy_runtime_enabled() is False
