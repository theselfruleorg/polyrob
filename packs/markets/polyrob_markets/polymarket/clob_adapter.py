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

The vendor import is LAZY (PEP 562): ``py_clob_client_v2`` costs ~250 ms and was
loading on every process boot via ``tools/__init__``. The first touch of any exported
symbol (``ClobClient``, ``CLOB_AVAILABLE``, …) or of ``clob_available()`` /
``trade_capability()`` / ``require_clob()`` performs the one real import; the
LOUD-failure contract is unchanged — only WHEN the import happens moves.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.exceptions import ToolError

INSTALL_HINT = "pip install py-clob-client-v2"

# Names materialized into module globals by _ensure_loaded() on first touch.
_LAZY_NAMES = (
    "ClobClient",
    "ApiCreds",
    "OrderArgs",
    "BalanceAllowanceParams",
    "AssetType",
    "CLOB_AVAILABLE",
    "CLOB_IMPORT_ERROR",
    "CLOB_VERSION",
)


def _ensure_loaded() -> None:
    """Perform the one real ``py_clob_client_v2`` import (idempotent)."""
    g = globals()
    if "CLOB_AVAILABLE" in g:
        return
    # The archived ``py_clob_client`` is intentionally NOT imported here — it is no
    # longer functional. Only the maintained v2 client is supported.
    try:
        from py_clob_client_v2.client import ClobClient  # type: ignore
        from py_clob_client_v2.clob_types import (  # type: ignore
            ApiCreds,
            OrderArgs,
            BalanceAllowanceParams,
            AssetType,
        )

        g["ClobClient"] = ClobClient
        g["ApiCreds"] = ApiCreds
        g["OrderArgs"] = OrderArgs
        g["BalanceAllowanceParams"] = BalanceAllowanceParams
        g["AssetType"] = AssetType
        g["CLOB_AVAILABLE"] = True
        g["CLOB_IMPORT_ERROR"] = None
        try:  # version is best-effort, never load-bearing
            from importlib.metadata import version as _pkg_version

            g["CLOB_VERSION"] = _pkg_version("py-clob-client-v2")
        except Exception:  # pragma: no cover - metadata absent
            g["CLOB_VERSION"] = None
    except Exception as _exc:  # ImportError or any transitive failure
        for _name in ("ClobClient", "ApiCreds", "OrderArgs", "BalanceAllowanceParams", "AssetType"):
            g[_name] = None
        g["CLOB_AVAILABLE"] = False
        g["CLOB_IMPORT_ERROR"] = f"{type(_exc).__name__}: {_exc}"
        g["CLOB_VERSION"] = None


def __getattr__(name: str):
    if name in _LAZY_NAMES:
        _ensure_loaded()
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY_NAMES))


class PolymarketDependencyError(ToolError):
    """Raised when a Polymarket trade action needs the CLOB client but it is absent."""


def clob_available() -> bool:
    """True iff the maintained CLOB trading client is importable (loads on first call)."""
    _ensure_loaded()
    return bool(globals()["CLOB_AVAILABLE"])


def trade_capability() -> Dict[str, Any]:
    """Report whether the trade surface can function, with an actionable hint."""
    _ensure_loaded()
    if globals()["CLOB_AVAILABLE"]:
        return {
            "available": True,
            "reason": "py-clob-client-v2 importable",
            "install_hint": INSTALL_HINT,
            "version": globals()["CLOB_VERSION"],
        }
    return {
        "available": False,
        "reason": f"Polymarket trading client unavailable ({globals()['CLOB_IMPORT_ERROR']})",
        "install_hint": INSTALL_HINT,
        "version": None,
    }


def require_clob() -> None:
    """Raise a typed, actionable error if the CLOB trading client is missing."""
    _ensure_loaded()
    if not globals()["CLOB_AVAILABLE"]:
        raise PolymarketDependencyError(
            f"Polymarket trading requires py-clob-client-v2 ({INSTALL_HINT}); "
            f"import failed: {globals()['CLOB_IMPORT_ERROR']}"
        )
