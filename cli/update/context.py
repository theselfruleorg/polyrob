"""Resolve the paths `polyrob update` snapshots and restores.

Keeps the snapshot/rollback commands from hand-constructing paths: DBs come from the
manifest SSOT, snapshots live under ``<data_home>/snapshots`` (inside data-home, which
the code swap never touches), and config/identity are captured so a rollback restores
the whole user state — never just the DBs.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class UpdateContext:
    data_home: Path
    snapshots_root: Path
    config_paths: List[Path] = field(default_factory=list)
    dir_paths: List[Path] = field(default_factory=list)
    db_paths: List[Path] = field(default_factory=list)


_DB_PATH_LINE = re.compile(r"^\s*(?:export\s+)?DB_PATH\s*=\s*(.+?)\s*$")


def _read_db_path_from_env_file(env_file: Path) -> Optional[str]:
    """Parse ``DB_PATH=...`` out of a ``.env`` file (best-effort, first match wins)."""
    try:
        for line in env_file.read_text().splitlines():
            m = _DB_PATH_LINE.match(line)
            if m:
                val = m.group(1).strip().strip('"').strip("'")
                if val:
                    return val
    except Exception:
        pass
    return None


def _resolve_configured_db_path(
    config_paths: List[Path], data_home: Path
) -> Optional[Path]:
    """The REAL bot.db path the app uses: ``DB_PATH`` env, else the config ``.env``.

    Mirrors ``core/config.py``: a relative value anchors to ``POLYROB_DATA_DIR`` when
    set, else the data-home. Returns None when nothing pins it (the manifest's default
    layouts then cover it).
    """
    raw = os.getenv("DB_PATH")
    if not raw:
        for env_file in config_paths:
            raw = _read_db_path_from_env_file(env_file)
            if raw:
                break
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        anchor = os.getenv("POLYROB_DATA_DIR") or str(data_home)
        p = Path(anchor) / p
    return p.resolve()


def resolve_update_context(*, local: bool = True) -> UpdateContext:
    """Resolve snapshot inputs from the live runtime paths (fail-soft)."""
    from core.db_manifest import all_sqlite_dbs
    from core.runtime_paths import resolve_runtime_paths

    rp = resolve_runtime_paths(local=local)
    data_home = Path(rp.data_home)

    config_paths: List[Path] = []
    # Server installs keep secrets in ``code_root/config/.env.{production,development}``;
    # local installs keep them at the data-home ``.env``. Anchor the server candidates
    # to ``code_root/config`` explicitly (NOT rp.config_dir, which is the data-home in
    # the local resolution this always uses) so config is captured in BOTH postures and
    # a rollback restores it (§2.4).
    server_config_dir = rp.code_root / "config"
    cfg_candidates = [
        data_home / ".env",
        rp.config_dir / ".env",
        server_config_dir / ".env",
        server_config_dir / ".env.production",
        server_config_dir / ".env.development",
    ]
    for cand in cfg_candidates:
        if cand.is_file() and cand not in config_paths:
            config_paths.append(cand)
    try:
        from core.paths import polyrob_home

        home_env = polyrob_home() / ".env"
        if home_env.is_file() and home_env not in config_paths:
            config_paths.append(home_env)
    except Exception:
        pass

    # Task 10: back up authored/installed skills too. Since Task 8/9 moved user
    # skills out of the code tree into <data_home>/skills, a code swap (pip -U) no
    # longer touches them — but they must ride the snapshot so update/rollback
    # preserves them alongside identity/.
    dir_paths: List[Path] = [p for p in (data_home / "identity", data_home / "skills") if p.is_dir()]

    # Resolve the REAL bot.db path from config (not a guessed layout). Pass it as an
    # explicit extra so it is captured even when it lives OUTSIDE data_home (prod:
    # /opt/polyrob/data/database/bot.db). The manifest's default layouts still cover
    # the un-configured case. (§2.6 — data-loss guard.)
    configured_db = _resolve_configured_db_path(config_paths, data_home)
    extra = [configured_db] if configured_db else None
    db_paths = all_sqlite_dbs(data_home, extra_dbs=extra)

    return UpdateContext(
        data_home=data_home,
        snapshots_root=data_home / "snapshots",
        config_paths=config_paths,
        dir_paths=dir_paths,
        db_paths=db_paths,
    )
