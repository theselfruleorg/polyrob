"""tools.defi.book.read_book — one reader over every money chain (043 A34).

The loop over chains lives here (tools tier), not core/book.py: it drives
DefiDataTool.portfolio/reconcile, and core may not import tools.*
(tests/test_layering_ratchet.py). webview/pages.py::api_book, and a later
REPL /book verb, both call this SAME function so the two surfaces can never
diverge.
"""
import pytest


class _AR:
    """Fake ActionResult — mirrors tests/unit/webview/test_positions_verdict.py."""

    def __init__(self, content=None, error=None, metadata=None):
        self.extracted_content = content
        self.error = error
        self.metadata = metadata


def _enable_defi(monkeypatch):
    import tools.defi as defi_pkg
    monkeypatch.setattr(defi_pkg, "defi_data_enabled", lambda: True)


@pytest.mark.asyncio
async def test_book_worst_chain_wins(monkeypatch, tmp_path):
    _enable_defi(monkeypatch)

    async def fake_reconcile(self, params, execution_context=None):
        if params.chain == "base":
            return _AR(metadata={"report": {"unbacked": ["BOTS"],
                                             "verdict": "DISAGREEMENT"}})
        raise RuntimeError("node down")

    async def fake_portfolio(self, params, execution_context=None):
        return _AR(content="holdings text")

    from tools.defi.data_tool import DefiDataTool
    monkeypatch.setattr(DefiDataTool, "reconcile", fake_reconcile)
    monkeypatch.setattr(DefiDataTool, "portfolio", fake_portfolio)

    ledger = tmp_path / "ledger.md"
    ledger.write_text("## Open positions\n")

    from tools.defi.book import read_book
    body = await read_book("u1", str(tmp_path), chains=["base", "solana"],
                            ledger_path=str(ledger))

    assert body["verdict"] == "disagreement"
    assert body["chains"]["base"]["verdict"] == "disagreement"
    assert body["chains"]["base"]["report"]["unbacked"] == ["BOTS"]
    assert body["chains"]["solana"]["verdict"] == "unverified"
    assert "node down" in body["chains"]["solana"]["error"]
    assert body["ledger_path"] == str(ledger)
    assert isinstance(body["checked_at"], float)


@pytest.mark.asyncio
async def test_book_clean_when_every_chain_agrees(monkeypatch, tmp_path):
    _enable_defi(monkeypatch)

    async def fake_reconcile(self, params, execution_context=None):
        return _AR(metadata={"report": {"verdict": "CLEAN"}})

    async def fake_portfolio(self, params, execution_context=None):
        return _AR(content="holdings text")

    from tools.defi.data_tool import DefiDataTool
    monkeypatch.setattr(DefiDataTool, "reconcile", fake_reconcile)
    monkeypatch.setattr(DefiDataTool, "portfolio", fake_portfolio)

    ledger = tmp_path / "ledger.md"
    ledger.write_text("## Open positions\n")

    from tools.defi.book import read_book
    body = await read_book("u1", str(tmp_path), chains=["base", "solana"],
                            ledger_path=str(ledger))

    assert body["verdict"] == "clean"
    assert body["chains"]["base"]["verdict"] == "clean"
    assert body["chains"]["solana"]["verdict"] == "clean"


@pytest.mark.asyncio
async def test_book_no_ledger_every_chain_and_skips_reconcile(monkeypatch, tmp_path):
    _enable_defi(monkeypatch)

    async def fake_portfolio(self, params, execution_context=None):
        return _AR(content="holdings text")

    called = {"reconcile": False}

    async def fake_reconcile(self, params, execution_context=None):
        called["reconcile"] = True
        return _AR(content="should never run")

    from tools.defi.data_tool import DefiDataTool
    monkeypatch.setattr(DefiDataTool, "portfolio", fake_portfolio)
    monkeypatch.setattr(DefiDataTool, "reconcile", fake_reconcile)

    from tools.defi.book import read_book
    body = await read_book("u1", str(tmp_path), chains=["base", "solana"],
                            ledger_path=None)

    assert body["verdict"] == "no_ledger"
    assert body["chains"]["base"]["verdict"] == "no_ledger"
    assert body["chains"]["solana"]["verdict"] == "no_ledger"
    assert body["chains"]["base"]["portfolio_text"] == "holdings text"
    assert called["reconcile"] is False
    assert body["ledger_path"] is None


@pytest.mark.asyncio
async def test_book_disabled_flag_returns_honest_error(monkeypatch, tmp_path):
    import tools.defi as defi_pkg
    monkeypatch.setattr(defi_pkg, "defi_data_enabled", lambda: False)

    from tools.defi.book import read_book
    body = await read_book("u1", str(tmp_path))

    assert body["chains"] == {}
    assert body["verdict"] is None
    assert "DEFI_DATA_ENABLED is off" in body["error"]
