"""T2 — service.py sources the CLOB client from the adapter and fails LOUD.

Replaces the archived `py_clob_client` import + silent `CLOB_CLIENT_AVAILABLE`
degrade with the single `clob_adapter` seam and a typed client-missing error.
The adapter loads the vendor SDK lazily (first touch), so the service reads
availability through ``clob_available()`` and imports the class symbols at USE
time — the seam is still the adapter module, now probed via monkeypatching the
adapter's state instead of module-level copies in service.py.
"""
import types
import pytest

import tools.polymarket.service as svc
import tools.polymarket.clob_adapter as ad
from tools.polymarket.service import PolymarketTool, PlaceLimitOrderParams


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


def test_service_sources_client_symbols_from_adapter():
    # The trade client symbols must come from the adapter (single seam), not a
    # private legacy import: service.py must not import the vendor package
    # directly, and its availability gate must be the adapter's.
    import inspect
    source = inspect.getsource(svc)
    # Vendor IMPORT lives ONLY in the adapter (comments may mention the name).
    assert "from py_clob_client" not in source
    assert "import py_clob_client" not in source
    assert "from tools.polymarket.clob_adapter import" in source
    assert svc.clob_available is ad.clob_available


def test_legacy_py_clob_client_not_imported():
    import inspect
    source = inspect.getsource(svc)
    assert "from py_clob_client." not in source  # archived/non-functional


def test_adapter_lazy_import_stays_off_boot_path():
    # Importing the service (what tools/__init__ does at boot) must not load the
    # vendor SDK — that is the whole point of the lazy adapter.
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys, tools.polymarket.service; "
         "print(any(m.startswith('py_clob_client_v2') for m in sys.modules))"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


@pytest.mark.asyncio
async def test_place_limit_order_loud_when_client_missing(monkeypatch):
    monkeypatch.setattr(ad, "CLOB_AVAILABLE", False)
    monkeypatch.setattr(ad, "CLOB_IMPORT_ERROR", "No module named 'py_clob_client_v2'")
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))

    res = await tool.place_limit_order(PlaceLimitOrderParams(
        market_id="m1", token_id="t1", side="buy", price=0.5, size_usd=5.0,
    ))
    assert res["success"] is False
    assert res.get("error_code") == "POLYMARKET_CLIENT_MISSING"
    assert "py-clob-client-v2" in (res.get("error", "") + res.get("suggestion", ""))
