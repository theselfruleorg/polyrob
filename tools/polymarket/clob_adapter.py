"""Single seam for the Polymarket CLOB trading client.

Polymarket archived `py-clob-client` in 2026 ("no longer functional"). The current
maintained low-level client is `py-clob-client-v2`. This module is the ONE place that
import happens, so a missing/incompatible client is a LOUD, actionable failure instead
of silently dropping every trading action (the prior behavior, which degraded the tool
to read-only with no signal).

Contract:
- The READ surface never imports this module.
- The TRADE surface calls ``require_clob()`` before any order action; absence yields a
  typed ``PolymarketDependencyError`` with an install hint, surfaced to the operator.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.exceptions import ToolError

INSTALL_HINT = "pip install py-clob-client-v2"

# The archived ``py_clob_client`` is intentionally NOT imported here — it is no longer
# functional. Only the maintained v2 client is supported.
try:
    from py_clob_client_v2.client import ClobClient  # type: ignore
    from py_clob_client_v2.clob_types import (  # type: ignore
        ApiCreds,
        OrderArgs,
        OrderType,
        BalanceAllowanceParams,
        AssetType,
    )

    CLOB_AVAILABLE = True
    CLOB_IMPORT_ERROR: Optional[str] = None
    try:  # version is best-effort, never load-bearing
        from importlib.metadata import version as _pkg_version

        CLOB_VERSION: Optional[str] = _pkg_version("py-clob-client-v2")
    except Exception:  # pragma: no cover - metadata absent
        CLOB_VERSION = None
except Exception as _exc:  # ImportError or any transitive failure
    ClobClient = None  # type: ignore
    ApiCreds = None  # type: ignore
    OrderArgs = None  # type: ignore
    OrderType = None  # type: ignore
    BalanceAllowanceParams = None  # type: ignore
    AssetType = None  # type: ignore
    CLOB_AVAILABLE = False
    CLOB_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"
    CLOB_VERSION = None


class PolymarketDependencyError(ToolError):
    """Raised when a Polymarket trade action needs the CLOB client but it is absent."""


def trade_capability() -> Dict[str, Any]:
    """Report whether the trade surface can function, with an actionable hint."""
    if CLOB_AVAILABLE:
        return {"available": True, "reason": "py-clob-client-v2 importable", "install_hint": INSTALL_HINT, "version": CLOB_VERSION}
    return {
        "available": False,
        "reason": f"Polymarket trading client unavailable ({CLOB_IMPORT_ERROR})",
        "install_hint": INSTALL_HINT,
        "version": None,
    }


def require_clob() -> None:
    """Raise a typed, actionable error if the CLOB trading client is missing."""
    if not CLOB_AVAILABLE:
        raise PolymarketDependencyError(
            f"Polymarket trading requires py-clob-client-v2 ({INSTALL_HINT}); "
            f"import failed: {CLOB_IMPORT_ERROR}"
        )
