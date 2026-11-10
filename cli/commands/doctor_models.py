"""``polyrob doctor``: a configured model with no exact catalog row.

With no row a model runs on its family's limits or a conservative default, and its
cost is unknown. Doctor names each such model, where it is set and what
that means. The stores are read read-only; an unreadable store is named, never read as
empty.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from core.sqlite_util import read_only_connect

_LABEL = "model catalog"
_LIVE_SESSION_STATUSES = ("created", "running", "resumed")


def _env_models(env: Mapping[str, str]) -> Iterable[Tuple[str, str]]:
    from agents.task.constants import parse_provider_model
    from core.runtime_config import heavy_model_pin, operator_model_pin
    for source, model in (("DEFAULT_MODEL", operator_model_pin(env)),
                          ("HEAVY_MODEL", heavy_model_pin(env))):
        if model:
            yield source, model
    for name in sorted(env):
        if name.startswith("AUX_MODEL_") and (env.get(name) or "").strip():
            yield name, env[name].strip()
        elif name.startswith("AUX_FALLBACK_"):
            for token in (env.get(name) or "").split(","):
                parsed = parse_provider_model(token)
                if parsed:
                    yield name, parsed["model"]


def _payload_models(path: str, sql: str, source: str) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """``(source, model)`` pairs of the live rows in a job store, or the read error."""
    if not os.path.isfile(path):
        return [], None
    try:
        con = read_only_connect(path)
        try:
            rows = con.execute(sql).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return [], f"! {_LABEL}: could not read {os.path.basename(path)} ({exc})"
    found = []
    for row_id, payload in rows:
        try:
            model = (json.loads(payload or "{}") or {}).get("model")
        except (TypeError, ValueError, AttributeError):
            continue
        if model:
            found.append((f"{source} {row_id}", str(model)))
    return found, None


def _default_pin_models(data_home: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """``(source, model)`` of each tenant's default model (``model.json``), and one line
    per file that cannot be read."""
    found, errors = [], []
    pattern = os.path.join(glob.escape(data_home), "identity", "*", "user_*", "model.json")
    for path in sorted(glob.glob(pattern)):
        tenant = os.path.basename(os.path.dirname(path))
        try:
            with open(path, encoding="utf-8") as handle:
                model = ((json.load(handle) or {}).get("default") or {}).get("model")
        except (OSError, ValueError, AttributeError) as exc:
            errors.append(f"! {_LABEL}: could not read the default model of {tenant} ({type(exc).__name__})")
            continue
        if model:
            found.append((f"default of {tenant}", str(model)))
    return found, errors


def _session_pin_models(session_root: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """``(source, model)`` of each live session's stored model, and one line per session
    record that cannot be read. A finished session runs nothing, so it is not read."""
    found, errors = [], []
    root = glob.escape(session_root)
    for pattern in ("*/*/metadata.json", "*/sessions/*/metadata.json"):
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            session = os.path.basename(os.path.dirname(path))
            try:
                with open(path, encoding="utf-8") as handle:
                    info = json.load(handle)
                if info.get("status") not in _LIVE_SESSION_STATUSES:
                    continue
                model = (info.get("request") or {}).get("model")
            except (OSError, ValueError, AttributeError) as exc:
                errors.append(f"! {_LABEL}: could not read session {session} ({type(exc).__name__})")
                continue
            if model:
                found.append((f"session {session}", str(model)))
    return found, errors


def _declared_provider_models() -> Tuple[List[Tuple[str, str]], List[str]]:
    """``(source, model)`` of each model a ``providers.yaml`` row declares."""
    from modules.llm.provider_spec import BUILTIN_SPECS, get_specs
    builtin = {spec.name for spec in BUILTIN_SPECS}
    try:
        specs = [spec for spec in get_specs() if spec.name not in builtin]
    except Exception as exc:  # noqa: BLE001 - a broken file is named, not read as empty
        return [], [f"! {_LABEL}: could not read providers.yaml ({type(exc).__name__})"]
    return [(f"providers.yaml {spec.name}", model) for spec in specs
            for model in dict.fromkeys([*spec.models, spec.default_model]) if model], []


def _consequence(model: str) -> str:
    """What running *model* with no row means, with the limits it reads."""
    from modules.llm.model_registry import get_model_config
    from modules.llm.model_types import model_family
    config = get_model_config(model)
    limits = f"{config.context_window} context and {config.max_completion_tokens} output tokens"
    if model_family(model):
        return f"it reads its family's limits ({limits}), and its cost is a family guess or $0"
    return f"it reads conservative limits ({limits}), and its cost records as $0"


def uncatalogued_model_lines(env: Mapping[str, str], data_home: str) -> List[str]:
    """One ``!`` line per configured model with no exact catalog row, and one per unreadable
    job store. Nothing to report is no line at all."""
    from core.goal_vocab import LIVE_STATUS_ORDER
    from core.runtime_paths import cron_db_path, goals_db_path
    from modules.llm.model_registry import get_model_config_exact

    pairs = list(_env_models(env))
    lines: List[str] = []
    for path, sql, source in (
        (cron_db_path(data_home), "SELECT id, payload FROM cron_jobs WHERE enabled=1", "job"),
        (goals_db_path(data_home), "SELECT id, payload FROM goals WHERE status IN (%s)"
         % ",".join(f"'{status}'" for status in LIVE_STATUS_ORDER), "goal"),
    ):
        found, error = _payload_models(path, sql, source)
        pairs.extend(found)
        if error:
            lines.append(error)
    for found, errors in (_default_pin_models(data_home),
                          _session_pin_models(env.get("DATA_ROOT") or os.path.join(data_home, "sessions")),
                          _declared_provider_models()):
        pairs.extend(found)
        lines.extend(errors)
    missing: Dict[str, List[str]] = {}
    for source, model in pairs:
        if get_model_config_exact(model) is None:
            missing.setdefault(model, []).append(source)
    for model, sources in missing.items():
        lines.append(f"! {_LABEL}: {model} has no exact row ({', '.join(sources)}): "
                     f"{_consequence(model)}")
    return lines
