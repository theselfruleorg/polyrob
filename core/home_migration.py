"""core/home_migration.py — one-time ``~/.rob`` -> ``~/.polyrob`` home migration.

Copy-not-move, marker-gated, fail-open. Protects an existing operator's
``~/.rob/.env`` (provider keys), ``cli.json`` (model choice), ``mcp.json`` and
``history`` when the framework config-home is renamed (doc 02). Non-destructive:
the legacy ``~/.rob`` is left intact and also remains a read-only fallback in
``core/bootstrap.load_env``.

Dependency-light, ``os``/``pathlib`` only — no ``from __future__ import
annotations`` (it imports nothing on the registry-closure path).
"""

import logging
import shutil
from pathlib import Path

from core.paths import polyrob_home

logger = logging.getLogger(__name__)

_MARKER_NAME = ".migrated_from_rob"


def migrate_rob_home_once() -> None:
    """Copy ``~/.rob`` -> ``~/.polyrob`` once, if the new home is absent.

    Gate: ``~/.polyrob`` missing AND ``~/.rob`` present. Idempotent (once the new
    home exists we never copy again) and fail-open (a copy error logs and proceeds
    with a fresh, usable ``~/.polyrob`` — never raises). Respects ``POLYROB_HOME``
    (migrates into whatever ``polyrob_home()`` resolves to).
    """
    try:
        new_home = polyrob_home()
        legacy_home = Path.home() / ".rob"

        if new_home.exists():
            return  # already migrated (or a fresh ~/.polyrob already exists)
        if not legacy_home.exists():
            return  # nothing to migrate — first-ever run

        try:
            shutil.copytree(str(legacy_home), str(new_home))
        except Exception as exc:
            logger.warning(
                "polyrob home migration copy failed (%s); proceeding with a fresh %s",
                exc, new_home,
            )
            # Fail-open: still leave a usable ~/.polyrob so the rest of boot works.
            try:
                new_home.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return

        # One-time marker so a future run is a guaranteed no-op even if someone
        # later empties ~/.polyrob (the new-home-exists gate already covers it).
        try:
            (new_home / _MARKER_NAME).write_text("migrated from ~/.rob\n")
        except Exception:
            pass
    except Exception as exc:  # absolute backstop — migration must never break boot
        logger.debug("polyrob home migration skipped: %s", exc)
