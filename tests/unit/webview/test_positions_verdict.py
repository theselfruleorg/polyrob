"""/positions verdict — a typed value, not a regex over prose (043 A34).

Before this, the page decided green/red by testing the reconcile verb's
rendered TEXT against `/DISAGREEMENT/i` then `/AGREEMENT|zero disagreements|
AGREES/i`. The verb's always-present closing footer ("If it dis**agrees**
with what you believed...") contains the string "agrees", so a report whose
verdict was actually UNVERIFIED (every balance read failed) still matched the
green branch and rendered "IN AGREEMENT". This file pins two things: the
regex is gone from the template, and the JSON endpoint carries a real
`verdict` field derived from the report's own lists.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _AR:
    def __init__(self, content=None, error=None, metadata=None):
        self.extracted_content = content
        self.error = error
        self.metadata = metadata


class _FakeTool:
    def __init__(self, *a, **k):
        pass

    async def portfolio(self, params, execution_context=None):
        return _AR(content="holdings for 0xME")

    async def reconcile(self, params, execution_context=None):
        # Shaped exactly like the incident: an always-present footer would
        # have matched the old regex's AGREEMENT branch even though the
        # verdict is UNVERIFIED (every balance read failed).
        return _AR(
            content="VERDICT: UNVERIFIED\n...\nIf it disagrees with what you "
                     "believed, the belief is wrong.",
            metadata={"report": {"unknown": ["X"], "verdict": "UNVERIFIED"}})


class _ErrTool(_FakeTool):
    async def reconcile(self, params, execution_context=None):
        # The wallet-not-enabled / outside-data-home / no-Open-positions-
        # heading / oversized-file family of refusals in
        # DefiDataTool.reconcile: an ActionResult(error=...) with no report
        # to derive a verdict from.
        return _AR(content=None, error="agent wallet not enabled", metadata=None)


def _client(monkeypatch, tmp_path, user_id="u1"):
    import webview.pages as pages
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", user_id)
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


def test_api_positions_verdict_is_unverified_not_regex_guessed(monkeypatch, tmp_path):
    monkeypatch.setenv("DEFI_DATA_ENABLED", "true")

    import tools.defi as defi_pkg
    monkeypatch.setattr(defi_pkg, "defi_data_enabled", lambda: True)

    import core.wallet.factory as wallet_factory
    monkeypatch.setattr(wallet_factory, "get_agent_wallet", lambda: None)

    import tools.defi.data_tool as dt
    monkeypatch.setattr(dt, "DefiDataTool", _FakeTool)

    client, _ = _client(monkeypatch, tmp_path)
    ledger = tmp_path / "ledger.md"
    ledger.write_text("## Open positions\n")
    monkeypatch.setenv("POSITION_LEDGER_PATH", str(ledger))

    res = client.get("/api/webgate/positions?chain=base")
    assert res.status_code == 200
    data = res.json()
    # The old regex would have called this "IN AGREEMENT" — the footer text
    # alone matches /AGREEMENT|zero disagreements|AGREES/i.
    assert data["verdict"] == "unverified"
    assert data["rows"] == {"unknown": ["X"], "verdict": "UNVERIFIED"}


def test_api_positions_verdict_no_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("DEFI_DATA_ENABLED", "true")

    import tools.defi as defi_pkg
    monkeypatch.setattr(defi_pkg, "defi_data_enabled", lambda: True)

    import core.wallet.factory as wallet_factory
    monkeypatch.setattr(wallet_factory, "get_agent_wallet", lambda: None)

    import tools.defi.data_tool as dt
    monkeypatch.setattr(dt, "DefiDataTool", _FakeTool)

    import core.position_ledger as pl
    monkeypatch.setattr(pl, "resolve_position_ledger_path", lambda data_dir=None: None)

    client, _ = _client(monkeypatch, tmp_path)
    res = client.get("/api/webgate/positions?chain=base")
    assert res.status_code == 200
    data = res.json()
    assert data["verdict"] == "no_ledger"


def test_api_positions_verdict_on_reconcile_error_is_unverified_not_clean(
        monkeypatch, tmp_path):
    # Fix round 1 (review finding): an errored reconcile read used to leave
    # `d = {}`, and verdict_from_report({}, ledger_found=True) is CLEAN — the
    # chip rendered green "IN AGREEMENT" beside a visible error message. The
    # old regex rendered no chip at all here; this pins the honest state.
    monkeypatch.setenv("DEFI_DATA_ENABLED", "true")

    import tools.defi as defi_pkg
    monkeypatch.setattr(defi_pkg, "defi_data_enabled", lambda: True)

    import core.wallet.factory as wallet_factory
    monkeypatch.setattr(wallet_factory, "get_agent_wallet", lambda: None)

    import tools.defi.data_tool as dt
    monkeypatch.setattr(dt, "DefiDataTool", _ErrTool)

    client, _ = _client(monkeypatch, tmp_path)
    ledger = tmp_path / "ledger.md"
    ledger.write_text("## Open positions\n")
    monkeypatch.setenv("POSITION_LEDGER_PATH", str(ledger))

    res = client.get("/api/webgate/positions?chain=base")
    assert res.status_code == 200
    data = res.json()
    assert data["verdict"] == "unverified"
    assert data["reconcile"]["error"] == "agent wallet not enabled"
    assert data["rows"] == {}


def test_api_positions_verdict_none_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("DEFI_DATA_ENABLED", raising=False)
    client, _ = _client(monkeypatch, tmp_path)
    res = client.get("/api/webgate/positions")
    assert res.status_code == 200
    data = res.json()
    assert data["verdict"] is None
