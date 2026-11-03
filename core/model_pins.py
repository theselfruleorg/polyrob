"""094: the owner's model choices that outlive a session — the default model for
every new owner chat (with its reasoning level) and the owner's own aliases.

ONE file per tenant, beside ``preferences.toml``:
``identity/{instance}/user_{uid}/model.json``. It is not a ``PREF_SCHEMA`` key
because a pin is a structured pair (provider + model + reasoning), not one scalar.
The per-SESSION pin is not here: it is the session record's ``request.model`` /
``request.provider`` (+ ``model_pin``), so it survives a restart and ends at ``/new``.

Reads distinguish "nothing set" from "could not read" (``load`` returns the error).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_NAME = "model.json"
MAX_ALIASES = 50


def _path(user_id: Optional[str], home_dir=None) -> Optional[Path]:
    from core.prefs import preferences_path
    if home_dir is None:
        from core.runtime_paths import prefs_home_dir
        home_dir = prefs_home_dir()
    pref = preferences_path(home_dir, user_id)
    return pref.parent / _NAME if pref is not None else None


def load(user_id: Optional[str], home_dir=None) -> Tuple[Dict, str]:
    """``(data, error)``. ``({}, "")`` = nothing set; ``({}, "<why>")`` = unreadable."""
    path = _path(user_id, home_dir)
    if path is None:
        return {}, "empty or unsafe user id"
    if not path.exists():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            return {}, f"{path} is not a JSON object"
        return data, ""
    except Exception as exc:  # noqa: BLE001 — an unreadable store is not an empty one
        logger.warning("model pins unreadable at %s: %s", path, exc)
        return {}, f"{path} could not be read ({type(exc).__name__})"


def _write(user_id: Optional[str], data: Dict, home_dir=None) -> Tuple[bool, str]:
    path = _path(user_id, home_dir)
    if path is None:
        return False, "empty or unsafe user id"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(str(tmp), str(path))
        return True, ""
    except Exception as exc:  # noqa: BLE001
        logger.error("model pins write failed (%s): %s", path, exc, exc_info=True)
        return False, f"write failed: {exc}"


def _update(user_id, home_dir, fn) -> Tuple[bool, str]:
    """Read-modify-write under a cross-process file lock (098 CL-05): an alias
    and a default written at the same time both survive."""
    path = _path(user_id, home_dir)
    if path is None:
        return False, "empty or unsafe user id"
    from core.state.files import file_lock
    with file_lock(str(path)):
        data, err = load(user_id, home_dir)
        if err:
            return False, err
        fn(data)
        return _write(user_id, data, home_dir)


def owner_default(user_id: Optional[str], home_dir=None) -> Optional[Dict]:
    """``{"provider", "model", "reasoning"?}`` or None (unset or unreadable — logged)."""
    data, _err = load(user_id, home_dir)
    row = data.get("default")
    if isinstance(row, dict) and row.get("provider") and row.get("model"):
        return row
    return None


def chat_pin_for(user_id: Optional[str], home_dir=None) -> Optional[Dict]:
    """The owner's default for a new owner chat, or None (no user / unset / unreadable)."""
    return owner_default(user_id, home_dir) if user_id else None


def chat_runtime_for(user_id: Optional[str], env=None, home_dir=None):
    """094, the CHAT tier: ``(provider, model, reasoning, source)`` a NEW owner chat
    starts on — the owner's default (``/model <name> default``) > the operator pin
    (``DEFAULT_PROVIDER``/``DEFAULT_MODEL``) > the first keyed provider. ``model`` may
    be None = the caller fills the provider's registry default. It lives here, not in
    ``core.runtime_config`` (which it calls), so the import runs one way. A live
    session's own pin is its session record, not read here."""
    from core.runtime_config import (canonicalize_provider, operator_model_pin,
                                     resolve_default_provider)
    pin = chat_pin_for(user_id, home_dir)
    if pin:
        return (canonicalize_provider(pin["provider"]) or pin["provider"], pin["model"],
                pin.get("reasoning"), "your default (/model <name> default)")
    provider, model = resolve_default_provider(env)[:2]
    source = "DEFAULT_MODEL (operator env)" if operator_model_pin(env) else "the provider default"
    _data, err = load(user_id, home_dir) if user_id else ({}, "")
    if err:  # an unreadable store is not an empty one: say so where a person reads it
        source += f" — ⚠️ your default could not be read: {err}"
    return provider, model, None, source


def set_default(user_id, provider: str, model: str, reasoning: Optional[str] = None,
                home_dir=None) -> Tuple[bool, str]:
    row = {"provider": provider, "model": model, "set_at": int(time.time())}
    if reasoning:
        row["reasoning"] = reasoning
    return _update(user_id, home_dir, lambda d: d.__setitem__("default", row))


def set_default_reasoning(user_id, reasoning: Optional[str], home_dir=None) -> Tuple[bool, str]:
    """Change only the default's reasoning level (no default model = nothing to change)."""
    def _fn(d):
        row = d.get("default")
        if isinstance(row, dict):
            if reasoning:
                row["reasoning"] = reasoning
            else:
                row.pop("reasoning", None)
    return _update(user_id, home_dir, _fn)


def clear_default(user_id, home_dir=None) -> Tuple[bool, str]:
    return _update(user_id, home_dir, lambda d: d.pop("default", None))


def aliases_read(user_id: Optional[str], home_dir=None) -> Tuple[Dict[str, str], str]:
    """The owner's aliases and the read error ('' when the store was read)."""
    data, err = load(user_id, home_dir)
    raw = data.get("aliases")
    return ({str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}), err


def aliases(user_id: Optional[str], home_dir=None) -> Dict[str, str]:
    return aliases_read(user_id, home_dir)[0]


def set_alias(user_id, name: str, target: Optional[str], home_dir=None) -> Tuple[bool, str]:
    """Add/replace an owner alias; ``target=None`` removes it."""
    def _fn(d):
        table = d.get("aliases") if isinstance(d.get("aliases"), dict) else {}
        if target:
            table[name] = target
        else:
            table.pop(name, None)
        d["aliases"] = table
    current = aliases(user_id, home_dir)
    if target and name not in current and len(current) >= MAX_ALIASES:
        return False, f"at most {MAX_ALIASES} aliases"
    return _update(user_id, home_dir, _fn)
