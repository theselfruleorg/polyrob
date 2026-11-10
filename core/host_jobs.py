"""Host-job status files: what a job the agent depends on, run OUTSIDE the agent, last did (112 W1).

A host job (a systemd timer, a cron line) writes ONE file per run:
``<data_home>/ops/host_jobs/<name>.json``::

    {"name": "…", "purpose": "≤200 chars", "ran_utc": "…Z", "ok": true,
     "max_age_h": 3, "errors": ["…"], "outputs": ["relative/path", …]}

Core knows the SHAPE, never a job: no job name lives here. ``core.status_loops`` reads the
directory into the ``loops`` section (a stale, failed or unreadable job is a health item →
``/status``, ``<live-health>``, ``agent_status``) and ``<operations>`` lists each job's purpose and
outputs, so the agent learns the job exists. ``scripts/ops/healthcheck.py`` alerts the owner.

Not named "heartbeat": that word means the in-process ``autonomy_tick`` telemetry
(``core.tickers``). Status is for visibility, never authority — no money gate reads it.

Stdlib only, so a host script loads this file by path. An unreadable file is a row that says
so, never a skipped one (an unreadable store is not an empty one).
"""
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, Optional

SUBDIR = ("ops", "host_jobs")
NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
TEXT_MAX = 200
MAX_ERRORS = 3
MAX_OUTPUTS = 10
MAX_FILES = 20
MAX_AGE_H_CEILING = 336.0
SKEW_SECONDS = 300
FILE_MODE = 0o640

#: Row states, in the order a reader should care about them.
OK, STALE, FAILED, UNREADABLE, SKEWED = "ok", "stale", "failed", "unreadable", "skewed"


def status_dir(data_home: str) -> str:
    return os.path.join(data_home, *SUBDIR)


def _clip(text) -> str:
    return " ".join(str(text or "").split())[:TEXT_MAX]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _safe_output(path) -> bool:
    p = str(path or "")
    return bool(p) and not p.startswith("/") and ".." not in p.replace("\\", "/").split("/")


def write_status(data_home: str, name: str, *, purpose: str, ok: bool, max_age_h: float,
                 errors: Optional[List[str]] = None, outputs: Optional[List[str]] = None,
                 now: Optional[datetime] = None) -> str:
    """Write *name*'s status atomically (tmp + ``os.replace``, mode 0640 so the agent's group
    reads a file another user wrote). Returns the path. Raises ``ValueError`` on a bad name."""
    if not NAME_RE.match(name or ""):
        raise ValueError(f"host job name must match {NAME_RE.pattern}")
    record = {
        "name": name, "purpose": _clip(purpose), "ok": bool(ok),
        "ran_utc": _iso(now or datetime.now(timezone.utc)),
        "max_age_h": float(max_age_h),
        "errors": [_clip(e) for e in (errors or [])][:MAX_ERRORS],
        "outputs": [str(o) for o in (outputs or []) if _safe_output(o)][:MAX_OUTPUTS],
    }
    folder = status_dir(data_home)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True))  # atomic: tmp + fsync + replace
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, FILE_MODE)
        path = os.path.join(folder, f"{name}.json")
        os.replace(tmp, path)
        return path
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _validate(stem: str, raw) -> Optional[str]:
    """Why *raw* is not a valid record for file *stem* (``None`` = valid)."""
    if not isinstance(raw, dict):
        return "not a JSON object"
    for key in ("name", "purpose", "ran_utc", "ok", "max_age_h", "errors", "outputs"):
        if key not in raw:
            return f"missing {key}"
    if raw["name"] != stem or not NAME_RE.match(str(raw["name"])):
        return "name does not match the file"
    if not isinstance(raw["ok"], bool):
        return "ok is not true/false"
    if _parse_iso(raw["ran_utc"]) is None:
        return "ran_utc is not an ISO time"
    try:
        age = float(raw["max_age_h"])
    except (TypeError, ValueError):
        return "max_age_h is not a number"
    if not 0 < age <= MAX_AGE_H_CEILING:
        return f"max_age_h must be in (0, {MAX_AGE_H_CEILING:g}]"
    if not isinstance(raw["errors"], list) or not isinstance(raw["outputs"], list):
        return "errors/outputs are not lists"
    if not all(_safe_output(o) for o in raw["outputs"]):
        return "an output path is not relative to the data home"
    return None


def read_statuses(data_home: str, now: Optional[datetime] = None) -> Dict[str, object]:
    """``{"available": bool, "error": str, "jobs": [row…]}`` for every status file.

    ``available=False`` with ``error`` = the directory itself could not be read. A missing
    directory = no host jobs (``available=True``, ``jobs=[]``). Each row:
    ``{name, state, purpose, ran_utc, age_h, max_age_h, errors, outputs, why}`` — an
    unreadable or invalid file is a row with ``state="unreadable"`` and the reason in ``why``.
    """
    now = now or datetime.now(timezone.utc)
    folder = status_dir(data_home)
    if not os.path.isdir(folder):
        return {"available": True, "error": "", "jobs": []}
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".json"))
    except OSError as e:
        return {"available": False, "error": f"cannot list {folder}: {e.strerror or e}",
                "jobs": []}
    jobs = []
    for fname in names[:MAX_FILES]:
        stem = fname[:-5]
        row = {"name": _clip(stem), "state": UNREADABLE, "purpose": "", "ran_utc": "",
               "age_h": None, "max_age_h": None, "errors": [], "outputs": [], "why": ""}
        try:
            with open(os.path.join(folder, fname), encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as e:
            row["why"] = _clip(f"cannot read: {getattr(e, 'strerror', None) or e}")
            jobs.append(row)
            continue
        bad = _validate(stem, raw)
        if bad:
            row["why"] = bad
            jobs.append(row)
            continue
        ran = _parse_iso(raw["ran_utc"])
        age_h = (now - ran).total_seconds() / 3600.0
        row.update(purpose=_clip(raw["purpose"]), ran_utc=_iso(ran), age_h=round(age_h, 2),
                   max_age_h=float(raw["max_age_h"]),
                   errors=[_clip(e) for e in raw["errors"]][:MAX_ERRORS],
                   outputs=[str(o) for o in raw["outputs"]][:MAX_OUTPUTS])
        if (ran - now).total_seconds() > SKEW_SECONDS:
            row.update(state=SKEWED, why="ran_utc is in the future (clock skew)")
        elif not raw["ok"]:
            row.update(state=FAILED, why="; ".join(row["errors"]) or "the job reported a failure")
        elif age_h > row["max_age_h"]:
            # No running age in the text: it rides the per-turn <operations>/<live-health>
            # delta, and a clock in it would re-push the delta every few minutes (112 review).
            row.update(state=STALE, why=f"no run since {row['ran_utc']} (expected every "
                                        f"{row['max_age_h']:g} h)")
        else:
            row["state"] = OK
        jobs.append(row)
    if len(names) > MAX_FILES:
        jobs.append({"name": "(more)", "state": UNREADABLE, "purpose": "", "ran_utc": "",
                     "age_h": None, "max_age_h": None, "errors": [], "outputs": [],
                     "why": f"{len(names) - MAX_FILES} more status files not read"})
    return {"available": True, "error": "", "jobs": jobs}
