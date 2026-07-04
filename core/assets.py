"""Location: core/assets.py

Webgate static-asset resolver (doc 01, T4).

Resolves the directory that holds the webgate's ``static/`` and ``templates/``
subdirectories. It prefers a packaged ``web_dist/`` bundle (shipped *inside* the
wheel, produced by doc 03) when present, and falls back to the repo ``webview/``
checkout when the bundle isn't built — so a dev tree is byte-identical to before
this seam existed.

The seam ships here; the bundle ships later (doc 03). Until then the packaged
probe finds nothing and the repo fallback is used — no behavior change.

Dependency-light, lives in ``core/`` (NOT on the action-registration import
path) so it carries no ``from __future__ import annotations`` landmine. Resolution
is fail-open: any probe error returns the repo fallback.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Repo dev fallback: <repo>/webview/ — the parent of this ``core/`` package
# joined with ``webview``. Holds ``static/`` and ``templates/`` (the dirs
# webview/server.py mounts), so the resolver's return value is the *base* dir and
# callers append the subdir.
_REPO_WEBVIEW_DIR = Path(__file__).resolve().parent.parent / "webview"


def _packaged_web_dist() -> Optional[Path]:
    """Return the packaged ``web_dist`` dir if it exists, else ``None``.

    Tries ``importlib.resources`` for an installed ``polyrob`` package first, then
    falls back to a ``__file__``-relative probe (covers a flat/editable layout
    where the bundle is dropped next to the install root). Fail-open: any error →
    ``None`` (caller uses the repo fallback).
    """
    # 1) importlib.resources against an installed top-level package.
    try:
        from importlib import resources

        for pkg in ("polyrob", "core"):
            try:
                root = resources.files(pkg)
            except (ModuleNotFoundError, ImportError, TypeError, AttributeError):
                continue
            # For the ``polyrob`` package the bundle is ``polyrob/web_dist``; for
            # the flat ``core`` package it sits beside it at ``<root>/web_dist``.
            base = Path(str(root))
            candidate = base / "web_dist" if pkg == "polyrob" else base.parent / "web_dist"
            try:
                if candidate.is_dir():
                    return candidate
            except OSError:
                continue
    except Exception:  # fail-open
        logger.debug("importlib.resources web_dist probe failed", exc_info=True)

    # 2) ``__file__``-relative probe (install/editable layouts).
    try:
        here = Path(__file__).resolve().parent  # <root>/core
        for candidate in (
            here.parent / "web_dist",
            here.parent / "polyrob" / "web_dist",
        ):
            if candidate.is_dir():
                return candidate
    except Exception:  # fail-open
        logger.debug("__file__-relative web_dist probe failed", exc_info=True)

    return None


def webgate_asset_dir() -> Path:
    """Resolve the dir holding the webgate ``static/`` and ``templates/``.

    Prefers a packaged ``web_dist/`` bundle; falls back to the repo ``webview/``.
    Logs which path it used.
    """
    packaged = _packaged_web_dist()
    if packaged is not None:
        logger.info("webgate assets: using packaged bundle at %s", packaged)
        return packaged
    logger.info("webgate assets: using repo fallback at %s", _REPO_WEBVIEW_DIR)
    return _REPO_WEBVIEW_DIR
