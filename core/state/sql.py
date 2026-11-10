"""Guarded SQL transitions (098 S2, S8).

A transition is ONE statement: ``UPDATE … SET status=:to … WHERE <key> AND
status IN (:from…) [AND <extra>]``; the caller acts on the row count. A CAS miss
is a normal result (S3): ``False``, logged as ``state.cas_miss``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Mapping, Optional, Tuple

from core.state.machine import StateMachine

logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    if not _IDENT.match(str(name)):
        raise ValueError(f"bad SQL identifier {name!r}")
    return name


def _build(table: str, *, key: str, key_params: tuple, status_col: str, to: Optional[str],
           froms: Tuple[str, ...], set_cols: Mapping[str, Any], json_set: Mapping[str, Any],
           json_col: str, where_extra: str, where_params: tuple):
    _ident(table)
    sets, params = [], []
    if to is not None:
        sets.append(f"{_ident(status_col)}=?")
        params.append(to)
    for col, val in dict(set_cols).items():
        sets.append(f"{_ident(col)}=?")
        params.append(val)
    if json_set:
        expr = f"COALESCE({_ident(json_col)}, '{{}}')"
        for k, v in dict(json_set).items():
            expr = f"json_set({expr}, ?, json(?))"
            params.extend([_json_path(k), json.dumps(v)])
        sets.append(f"{_ident(json_col)}={expr}")
    if not sets:
        raise ValueError("nothing to set")
    where = f"({key})"
    wparams = list(key_params)
    if froms:
        where += f" AND {_ident(status_col)} IN ({','.join('?' * len(froms))})"
        wparams.extend(froms)
    if where_extra:
        where += f" AND ({where_extra})"
        wparams.extend(where_params)
    sql = f"UPDATE {table} SET {', '.join(sets)} WHERE {where}"
    return sql, tuple(params + wparams)


def _json_path(k: str) -> str:
    k = str(k)
    return k if k.startswith("$") else "$." + k


def _froms(machine: StateMachine, to: str, from_states: Optional[Iterable[str]], ctx) -> tuple:
    """The stored values the WHERE accepts: every allowed from-state plus each
    legacy alias that normalizes to one (a row still holding ``approved`` for
    ``pending`` must move like ``pending``, not CAS-miss for ever)."""
    froms = machine.require_sources(to, from_states, ctx=ctx)
    legacy = sorted(a for a, s in machine.aliases.items() if s in froms and a not in froms)
    return tuple(froms) + tuple(legacy)


def cas_transition(db_path: str, table: str, *, key: str, key_params: tuple,
                   machine: StateMachine, to: str, from_states: Optional[Iterable[str]] = None,
                   set_cols: Mapping[str, Any] = None, json_set: Mapping[str, Any] = None,
                   json_col: str = "payload", where_extra: str = "", where_params: tuple = (),
                   status_col: str = "status") -> bool:
    """``from_states -> to`` in one guarded UPDATE. True when exactly one row moved.

    ``from_states=None`` means every source the machine lists for ``to``; an
    explicit list is checked against the table first, so a call site cannot ask
    for a forbidden edge. ``where_extra`` carries owner/revision/claim predicates.
    """
    from core.sqlite_util import execute_retry
    ctx = {"table": table, "key": key_params}
    froms = _froms(machine, to, from_states, ctx)
    if not froms:
        return False
    sql, params = _build(table, key=key, key_params=key_params, status_col=status_col,
                         to=machine.normalize(to), froms=froms, set_cols=set_cols or {},
                         json_set=json_set or {}, json_col=json_col, where_extra=where_extra,
                         where_params=where_params)
    rc = execute_retry(db_path, sql, params)
    if rc != 1:
        _cas_miss(machine.name, table, key_params, froms, to)
    return rc == 1


async def acas_transition(db: Any, table: str, *, key: str, key_params: tuple,
                          machine: StateMachine, to: str,
                          from_states: Optional[Iterable[str]] = None,
                          set_cols: Mapping[str, Any] = None, json_set: Mapping[str, Any] = None,
                          json_col: str = "payload", where_extra: str = "",
                          where_params: tuple = (), status_col: str = "status") -> bool:
    """The async variant for ``bot.db``: runs on the caller's ``db`` (inside its
    ``begin_transaction`` span when one is open) and opens no connection.
    ``db.execute`` must return a cursor-like object with ``rowcount`` (or the
    row count itself)."""
    ctx = {"table": table, "key": key_params}
    froms = _froms(machine, to, from_states, ctx)
    if not froms:
        return False
    sql, params = _build(table, key=key, key_params=key_params, status_col=status_col,
                         to=machine.normalize(to), froms=froms, set_cols=set_cols or {},
                         json_set=json_set or {}, json_col=json_col, where_extra=where_extra,
                         where_params=where_params)
    res = await db.execute(sql, params)
    rc = res if isinstance(res, int) else getattr(res, "rowcount", 0)
    if rc != 1:
        _cas_miss(machine.name, table, key_params, froms, to)
    return rc == 1


def json_patch(db_path: str, table: str, *, key: str, key_params: tuple,
               json_col: str = "payload", set: Mapping[str, Any] = None,
               where_extra: str = "", where_params: tuple = ()) -> bool:
    """Update JSON keys in place (S8) without a status move: the writer changes
    only the keys it owns, never the whole document."""
    from core.sqlite_util import execute_retry
    if not set:
        raise ValueError("nothing to set")
    sql, params = _build(table, key=key, key_params=key_params, status_col="status", to=None,
                         froms=(), set_cols={}, json_set=set, json_col=json_col,
                         where_extra=where_extra, where_params=where_params)
    return execute_retry(db_path, sql, params) == 1


def _cas_miss(machine: str, table: str, key_params, froms, to) -> None:
    logger.info("state.cas_miss %s %s %s: expected %s -> %s", machine, table, key_params,
                list(froms), to)
