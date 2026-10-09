"""The install record — what the bootstrap actually did, written once.

``<data home>/.polyrob-bootstrap.json`` answers three questions no surface
could answer before: was this install ever bootstrapped, HOW was it installed,
and which optional stages ran or were skipped and why.

Three rules, all learned the hard way elsewhere in this tree:

* **Atomic or absent.** The file is written to a sibling temp path and
  ``os.replace``d. A torn marker is worse than no marker, because "absent"
  reads as "never bootstrapped" and a half-written one reads as a lie.
* **Unreadable is its own answer.** :func:`read_marker` distinguishes
  ``absent`` from ``unreadable``; a caller must never render a damaged record
  as "not installed" (the confident-zero class this repo keeps re-learning).
* **One writer.** ``polyrob init`` calls :func:`write_marker`. ``install.sh``
  hands its stage list in through ``POLYROB_BOOTSTRAP_STAGES`` rather than
  writing the file itself, so the format has exactly one author.

⚠️ The record lives in the **config home** (``core.paths.polyrob_home`` —
``~/.polyrob``), NOT the data home. POLYROB's local data home is
``cwd/.polyrob`` BY DESIGN (a per-project memory, see
``core.runtime_paths.resolve_runtime_paths``), and an install fact written
there would read as "never bootstrapped" from every other directory. A live
install run on 2026-09-22 did exactly that before this was fixed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1
MARKER_NAME = ".polyrob-bootstrap.json"

#: install.sh -> polyrob init. A JSON object with ``install_method``,
#: ``source_dir``, ``venv_dir``, ``command`` and ``stages``.
STAGES_ENV = "POLYROB_BOOTSTRAP_STAGES"

#: Status values from :func:`read_marker`.
ABSENT = "absent"
OK = "ok"
UNREADABLE = "unreadable"


def install_home() -> Path:
    """The per-user config home the record belongs in."""
    from core.paths import polyrob_home
    return polyrob_home()


def marker_path(data_home: Optional[Path | str] = None) -> Path:
    base = Path(data_home) if data_home is not None else install_home()
    return base / MARKER_NAME


def stages_from_env(env: Optional[dict] = None) -> dict:
    """Parse the installer's handoff. Anything malformed yields ``{}`` — the
    marker is still written, just without the installer's half."""
    raw = (env if env is not None else os.environ).get(STAGES_ENV) or ""
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_marker(data_home: Optional[Path | str] = None, *, version: str = "",
                 install_method: str = "", extras: Optional[list] = None,
                 soul_seed_sha: str = "", installer: Optional[dict] = None,
                 env: Optional[dict] = None) -> Optional[Path]:
    """Write the record. Returns the path, or None when it could not be written
    (never raises — a failed marker must not fail an install)."""
    try:
        home = Path(data_home) if data_home is not None else install_home()
        home.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "version": version,
            "install_method": install_method,
            "extras": sorted(extras or []),
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if soul_seed_sha:
            payload["soul_seed_sha256"] = soul_seed_sha
        from_installer = installer if installer is not None else stages_from_env(env)
        if from_installer:
            # Only the keys we document — an installer typo must not become a
            # schema field every later reader has to tolerate.
            for key in ("install_method", "source_dir", "venv_dir", "command"):
                value = from_installer.get(key)
                if isinstance(value, str) and value:
                    payload[key] = value
            stages = from_installer.get("stages")
            if isinstance(stages, list):
                payload["stages"] = [s for s in stages if isinstance(s, dict)]
        target = marker_path(home)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, target)
        return target
    except Exception:
        return None


def read_marker(data_home: Optional[Path | str] = None) -> tuple[str, Optional[dict]]:
    """``(status, payload)`` where status is ``absent`` / ``ok`` / ``unreadable``."""
    path = marker_path(data_home)
    try:
        if not path.is_file():
            return ABSENT, None
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ABSENT, None
    except Exception:
        return UNREADABLE, None
    if not isinstance(data, dict) or "schema_version" not in data:
        return UNREADABLE, None
    return OK, data


def describe(data_home: Optional[Path | str] = None) -> str:
    """One honest line for a status surface."""
    status, data = read_marker(data_home)
    if status == ABSENT:
        return "bootstrap: not recorded (run `polyrob setup`)"
    if status == UNREADABLE:
        return f"bootstrap: unreadable ({marker_path(data_home)})"
    assert data is not None
    method = data.get("install_method") or "unknown"
    when = data.get("completed_at") or "?"
    version = data.get("version") or "?"
    line = f"bootstrap: {method}, installed at v{version}, {when}"
    skipped = [s.get("name") for s in (data.get("stages") or [])
               if isinstance(s, dict) and s.get("status") == "skipped"]
    if skipped:
        line += f" (skipped: {', '.join(str(s) for s in skipped if s)})"
    return line


__all__ = ["SCHEMA_VERSION", "MARKER_NAME", "STAGES_ENV", "ABSENT", "OK",
           "UNREADABLE", "install_home", "marker_path", "stages_from_env",
           "write_marker", "read_marker", "describe"]
