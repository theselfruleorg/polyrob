"""Key-level writes to ``goals.payload`` (098 S8, AG-21/TS-14).

The payload holds state (``grant_consumed``, ``poller_gone``, ``decision``,
``blocks_goal_ids``, ``attempts`` …) that several writers change at once. A
writer that reads the document, edits a dict and writes ALL of it back loses
every key another writer set in between — a reset ``grant_consumed`` redeems a
one-shot grant twice. So every writer here changes only its own keys, with
``json_set``/``json_remove`` in ONE statement (with the status guard when there
is one). A capped list (``attempts``, ``owner_answers``) is a compare-and-set on
that one key: re-read and retry on a miss, never a blind overwrite.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable, List, Mapping, Optional, Tuple

from core.sqlite_util import execute_retry

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _path(key: str) -> str:
    if not _KEY.match(str(key)):
        raise ValueError(f"bad payload key {key!r}")
    return "$." + key


def payload_expr(set_keys: Optional[Mapping[str, Any]] = None,
                 remove: Iterable[str] = (), *, col: str = "payload") -> Tuple[str, list]:
    """``(sql_expression, params)`` for the new payload: *col* with *set_keys*
    set (values keep their JSON type) and *remove* keys dropped."""
    expr, params = f"COALESCE({col}, '{{}}')", []
    for k, v in dict(set_keys or {}).items():
        expr = f"json_set({expr}, ?, json(?))"
        params.extend([_path(k), json.dumps(v, default=str)])
    rm = [_path(k) for k in remove]
    if rm:
        expr = f"json_remove({expr}, {', '.join('?' * len(rm))})"
        params.extend(rm)
    return expr, params


def patch(db_path: str, row_id: str, *, set_keys: Optional[Mapping[str, Any]] = None,
          remove: Iterable[str] = (), where_extra: str = "",
          where_params: tuple = ()) -> bool:
    """Set/remove payload keys on one row; True when the row matched."""
    remove = list(remove)
    if not set_keys and not remove:
        return False
    expr, params = payload_expr(set_keys, remove)
    sql = f"UPDATE goals SET payload={expr} WHERE id=?"
    params.append(row_id)
    if where_extra:
        sql += f" AND ({where_extra})"
        params.extend(where_params)
    return execute_retry(db_path, sql, tuple(params)) == 1


def update_key_cas(db_path: str, row_id: str, key: str,
                   fn: Callable[[Any], Any], *, where_extra: str = "",
                   where_params: tuple = (), attempts: int = 8) -> bool:
    """Compute a new value of ONE key from its current value and write it only
    if the key still holds what was read (a CAS on that key; other keys are
    never touched). Retries on a miss; False when the row does not match."""
    path = _path(key)
    for _ in range(max(1, attempts)):
        row = execute_retry(
            db_path,
            "SELECT json_extract(COALESCE(payload,'{}'), ?) AS v FROM goals WHERE id=?"
            + (f" AND ({where_extra})" if where_extra else ""),
            (path, row_id, *where_params), fetch="one")
        if row is None:
            return False
        raw = row["v"]
        cur = _decode(raw)
        new = fn(cur)
        expr, params = payload_expr({key: new})
        guard = ("json_extract(COALESCE(payload,'{}'), ?) IS NULL" if raw is None
                 else "json_extract(COALESCE(payload,'{}'), ?) = ?")
        gparams: List[Any] = [path] if raw is None else [path, raw]
        sql = f"UPDATE goals SET payload={expr} WHERE id=? AND {guard}"
        params.append(row_id)
        params.extend(gparams)
        if where_extra:
            sql += f" AND ({where_extra})"
            params.extend(where_params)
        if execute_retry(db_path, sql, tuple(params)) == 1:
            return True
    return False


def append_capped(db_path: str, row_id: str, key: str, item: Any, cap: int, **kw) -> bool:
    """Append *item* to the list at *key*, keeping the newest *cap* entries."""
    def _fn(cur):
        lst = list(cur) if isinstance(cur, list) else []
        lst.append(item)
        return lst[-cap:] if cap > 0 else lst
    return update_key_cas(db_path, row_id, key, _fn, **kw)


def _decode(raw: Any) -> Any:
    """``json_extract`` returns SQL scalars as-is and arrays/objects as JSON text."""
    if isinstance(raw, str) and raw[:1] in ("[", "{"):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


__all__ = ["append_capped", "patch", "payload_expr", "update_key_cas"]
