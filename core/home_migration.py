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


_IDENTITY_MARKER_NAME = ".migrated_identity_rob"


def migrate_identity_instance_once(data_home=None) -> None:
    """Copy ``identity/rob`` -> ``identity/polyrob`` once (W1 neutral default).

    ``DEFAULT_INSTANCE_ID`` changed ``"rob"`` -> ``"polyrob"``; without this
    copy an existing install's SELF/owner docs would silently vanish behind the
    renamed directory. Copy-not-move, marker-gated, fail-open — the same shape
    as :func:`migrate_rob_home_once`.

    Runs ONLY when the default instance id is in effect: an explicit
    ``POLYROB_INSTANCE_ID``/``BOT_INSTANCE_ID`` (e.g. prod pinning ``rob``)
    keeps reading its own tree and needs no copy.
    """
    try:
        from core.instance import (DEFAULT_INSTANCE_ID, LEGACY_INSTANCE_ID,
                                   resolve_instance_id)
        if resolve_instance_id() != DEFAULT_INSTANCE_ID:
            return
        if data_home is None:
            from core.runtime_paths import resolve_data_home
            data_home = resolve_data_home()
        identity_root = Path(data_home) / "identity"
        new_tree = identity_root / DEFAULT_INSTANCE_ID
        legacy_tree = identity_root / LEGACY_INSTANCE_ID
        marker = identity_root / _IDENTITY_MARKER_NAME

        if new_tree.exists() or marker.exists():
            return  # already migrated (or a polyrob tree already exists)
        if not legacy_tree.is_dir():
            return  # nothing to migrate

        try:
            shutil.copytree(str(legacy_tree), str(new_tree))
        except Exception as exc:
            logger.warning(
                "identity instance migration copy failed (%s); the legacy %s tree "
                "is untouched", exc, legacy_tree)
            return

        try:
            marker.write_text("copied identity/rob -> identity/polyrob\n")
        except Exception:
            pass
        logger.info("migrated identity docs: %s -> %s (copy; source kept)",
                    legacy_tree, new_tree)
    except Exception as exc:  # absolute backstop — migration must never break boot
        logger.debug("identity instance migration skipped: %s", exc)
